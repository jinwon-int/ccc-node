"""Tests for polling watchdog and shutdown behavior."""

# ruff: noqa: E402
import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, PropertyMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ORIGINAL_PROJECT_ROOT = os.environ.get("PROJECT_ROOT")
_ORIGINAL_CONFIG_MODULE = sys.modules.get("telegram_bot.utils.config")
_ORIGINAL_PROJECT_CHAT_MODULE = sys.modules.get("telegram_bot.core.project_chat")
_ORIGINAL_CHAT_LOGGER_MODULE = sys.modules.get("telegram_bot.utils.chat_logger")
os.environ["PROJECT_ROOT"] = str(Path(__file__).resolve().parents[1])

from pathlib import Path as _Path

_TEST_BOT_DIR = _Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / "test_bot"

config_module = types.ModuleType("telegram_bot.utils.config")
setattr(
    config_module,
    "config",
    types.SimpleNamespace(
        telegram_bot_token="test_token",
        network_retry_attempts=3,
        network_retry_delay=5,
        polling_timeout=30,
        bot_data_dir=_TEST_BOT_DIR,
        logs_dir=_TEST_BOT_DIR / "logs",
        session_store_path=_TEST_BOT_DIR / "sessions.json",
        allowed_user_ids=[],
        draft_update_min_chars=150,
        draft_update_interval=1.0,
        ffmpeg_path=None,
        claude_cli_path=None,
        claude_settings_path=_Path.home() / ".claude" / "settings.json",
    ),
)
sys.modules["telegram_bot.utils.config"] = config_module

chat_logger_module = types.ModuleType("telegram_bot.utils.chat_logger")
chat_logger_module.log_debug = lambda *args, **kwargs: None
chat_logger_module.log_chat = lambda *args, **kwargs: None
sys.modules["telegram_bot.utils.chat_logger"] = chat_logger_module

project_chat_module = types.ModuleType("telegram_bot.core.project_chat")
project_chat_module.project_chat_handler = types.SimpleNamespace()
project_chat_module.ChatResponse = type("ChatResponse", (), {})
project_chat_module.PROJECT_ROOT = _Path(os.environ["PROJECT_ROOT"])
project_chat_module.CONVERSATIONS_DIR = _TEST_BOT_DIR / "conversations"
sys.modules["telegram_bot.core.project_chat"] = project_chat_module

_EXTRACTED_MODULE_NAMES = (
    "telegram_bot.core.bot_lifecycle",
    "telegram_bot.core.bot_status",
    "telegram_bot.core.bot_access",
    "telegram_bot.core.bot_commands",
    "telegram_bot.core.bot_delivery",
    "telegram_bot.core.bot_voice",
)
_ORIGINAL_EXTRACTED_MODULES = {
    name: sys.modules.get(name) for name in _EXTRACTED_MODULE_NAMES
}
_ORIGINAL_BOT_MODULE = sys.modules.get("telegram_bot.core.bot")
_ORIGINAL_EXTRACTED_PROJECT_CHAT_HANDLERS = {
    name: getattr(module, "project_chat_handler")
    for name in _EXTRACTED_MODULE_NAMES
    if (module := sys.modules.get(name)) is not None
    and hasattr(module, "project_chat_handler")
}

sys.modules.pop("telegram_bot.core.bot", None)
import telegram_bot.core.bot as bot_module

bot_module.health_reporter = types.SimpleNamespace(
    initialize_process=lambda: None,
    mark_starting=lambda reason=None: None,
    mark_unavailable=lambda reason=None: None,
    cleanup_runtime_files=lambda: None,
    record_telegram_ok=lambda: None,
    record_telegram_error=lambda error, consecutive_failures=None: None,
    record_claude_ok=lambda: None,
    record_claude_error=lambda error: None,
)
_FAKE_HEALTH_REPORTER = bot_module.health_reporter

from telegram_bot.core.bot_shared import _PollingRestart

if _ORIGINAL_PROJECT_ROOT is None:
    os.environ.pop("PROJECT_ROOT", None)
else:
    os.environ["PROJECT_ROOT"] = _ORIGINAL_PROJECT_ROOT

if _ORIGINAL_CONFIG_MODULE is None:
    sys.modules.pop("telegram_bot.utils.config", None)
else:
    sys.modules["telegram_bot.utils.config"] = _ORIGINAL_CONFIG_MODULE

if _ORIGINAL_PROJECT_CHAT_MODULE is None:
    sys.modules.pop("telegram_bot.core.project_chat", None)
else:
    sys.modules["telegram_bot.core.project_chat"] = _ORIGINAL_PROJECT_CHAT_MODULE

if _ORIGINAL_CHAT_LOGGER_MODULE is None:
    sys.modules.pop("telegram_bot.utils.chat_logger", None)
else:
    sys.modules["telegram_bot.utils.chat_logger"] = _ORIGINAL_CHAT_LOGGER_MODULE

for _module_name, _original_module in _ORIGINAL_EXTRACTED_MODULES.items():
    if _original_module is None:
        sys.modules.pop(_module_name, None)
    else:
        sys.modules[_module_name] = _original_module

if _ORIGINAL_BOT_MODULE is None:
    sys.modules.pop("telegram_bot.core.bot", None)
else:
    sys.modules["telegram_bot.core.bot"] = _ORIGINAL_BOT_MODULE


def _telegram_bot_module():
    chat_logger = sys.modules.get("telegram_bot.utils.chat_logger")
    if chat_logger is not None and not callable(getattr(chat_logger, "log_debug", None)):
        sys.modules.pop("telegram_bot.utils.chat_logger", None)
    import telegram_bot.core.bot as runtime_bot_module

    runtime_bot_module.health_reporter = _FAKE_HEALTH_REPORTER
    return runtime_bot_module


def _make_bot(**kwargs):
    return _telegram_bot_module().TelegramBot(**kwargs)


class TestPollingWatchdog(unittest.TestCase):
    """Test watchdog detection and polling restart."""

    def setUp(self):
        self.bot = _make_bot(
            settings=config_module.config,
            session_manager=Mock(),
            project_chat=project_chat_module.project_chat_handler,
        )

    def test_watchdog_healthy_api_no_restart(self):
        """Watchdog should keep running when Telegram API stays reachable."""
        stop_event = asyncio.Event()
        mock_app = Mock()
        mock_app.bot = Mock()
        mock_app.bot.get_me = AsyncMock(return_value=True)

        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)
        mock_updater.stop = AsyncMock()
        mock_app.updater = mock_updater

        self.bot.application = mock_app
        self.bot._WATCHDOG_INTERVAL = 0.01

        async def run():
            task = asyncio.create_task(self.bot._polling_watchdog(stop_event))
            await asyncio.sleep(0.03)
            stop_event.set()
            await task

        asyncio.run(run())

        self.assertGreaterEqual(mock_app.bot.get_me.await_count, 1)
        mock_updater.stop.assert_not_awaited()

    def test_watchdog_requests_restart_after_threshold(self):
        """Watchdog should stop the updater and raise _PollingRestart after prolonged failure."""
        stop_event = asyncio.Event()
        mock_app = Mock()
        mock_app.bot = Mock()
        mock_app.bot.get_me = AsyncMock(side_effect=Exception("timeout"))

        mock_updater = Mock()
        updater_state = {"running": True}
        type(mock_updater).running = PropertyMock(
            side_effect=lambda: updater_state["running"]
        )

        async def stop_updater():
            updater_state["running"] = False

        mock_updater.stop = AsyncMock(side_effect=stop_updater)
        mock_app.updater = mock_updater

        self.bot.application = mock_app
        self.bot._WATCHDOG_INTERVAL = 0.01
        self.bot._NETWORK_FAILURE_THRESHOLD = 0.02

        async def run():
            with self.assertRaises(_PollingRestart):
                await self.bot._polling_watchdog(stop_event)

        asyncio.run(run())

        self.assertGreaterEqual(mock_app.bot.get_me.await_count, 2)
        mock_updater.stop.assert_awaited_once()


class TestWaitForPollingExit(unittest.TestCase):
    """Test _wait_for_polling_exit detects terminal states."""

    def setUp(self):
        self.bot = _make_bot(
            settings=config_module.config,
            session_manager=Mock(),
            project_chat=project_chat_module.project_chat_handler,
        )

    def test_unexpected_exit_triggers_restart(self):
        """Polling exiting unexpectedly should raise _PollingRestart."""
        stop_event = asyncio.Event()
        mock_app = Mock()
        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=False)
        mock_app.updater = mock_updater

        self.bot.application = mock_app

        async def run():
            with self.assertRaises(_PollingRestart):
                await self.bot._wait_for_polling_exit(stop_event)

        asyncio.run(run())

    def test_stop_event_exits_cleanly(self):
        """Setting stop_event should let _wait_for_polling_exit return without restart."""
        stop_event = asyncio.Event()
        stop_event.set()
        mock_app = Mock()
        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)
        mock_app.updater = mock_updater

        self.bot.application = mock_app

        asyncio.run(self.bot._wait_for_polling_exit(stop_event))


class TestGracefulShutdown(unittest.TestCase):
    """Test _graceful_shutdown cleans up properly."""

    def setUp(self):
        self.bot = _make_bot(
            settings=config_module.config,
            session_manager=Mock(),
            project_chat=project_chat_module.project_chat_handler,
        )

    def test_shutdown_stops_all_components(self):
        """Graceful shutdown stops updater, app, and calls shutdown."""
        mock_app = Mock()
        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)
        mock_updater.stop = AsyncMock()
        mock_app.updater = mock_updater
        type(mock_app).running = PropertyMock(return_value=True)
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()

        self.bot.application = mock_app

        asyncio.run(self.bot._graceful_shutdown())

        mock_updater.stop.assert_awaited_once()
        mock_app.stop.assert_awaited_once()
        mock_app.shutdown.assert_awaited_once()
        self.assertIsNone(self.bot.application)

    def test_shutdown_noop_when_no_application(self):
        """Graceful shutdown is a no-op when application is None."""
        self.bot.application = None
        asyncio.run(self.bot._graceful_shutdown())
        self.assertIsNone(self.bot.application)

    def test_shutdown_handles_errors(self):
        """Graceful shutdown should swallow teardown errors and clear application state."""
        mock_app = Mock()
        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)
        mock_updater.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        mock_app.updater = mock_updater
        type(mock_app).running = PropertyMock(return_value=True)
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()

        self.bot.application = mock_app

        asyncio.run(self.bot._graceful_shutdown())

        self.assertIsNone(self.bot.application)


if __name__ == "__main__":
    unittest.main()
