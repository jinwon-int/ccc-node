"""Tests for Telegram bot connection resilience after system sleep."""

# ruff: noqa: E402
import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch, PropertyMock
from pathlib import Path
import sys
import types
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sys_modules_isolation import ModuleFakesGuard

_sys_modules_guard = ModuleFakesGuard(__name__).begin()

_ORIGINAL_PROJECT_ROOT = os.environ.get("PROJECT_ROOT")
_ORIGINAL_CONFIG_MODULE = sys.modules.get("telegram_bot.utils.config")
# Set PROJECT_ROOT before importing bot modules
os.environ["PROJECT_ROOT"] = str(Path(__file__).resolve().parents[1])

# Mock config module
from pathlib import Path as _Path

config_module = types.ModuleType("telegram_bot.utils.config")
setattr(
    config_module,
    "config",
    types.SimpleNamespace(
        telegram_bot_token="test_token",
        network_retry_attempts=3,
        network_retry_delay=5,
        polling_timeout=30,
        bot_data_dir=_Path("/tmp/test_bot"),
        logs_dir=_Path("/tmp/test_bot/logs"),
        session_store_path=_Path("/tmp/test_bot/sessions.json"),
        allowed_user_ids=[],
        require_allowlist=False,  # access-control guard not under test here
        draft_update_min_chars=150,
        draft_update_interval=1.0,
        ffmpeg_path=None,
        claude_cli_path=None,
        codex_cli_path="codex",
        agent_provider="claude",
        claude_settings_path=_Path.home() / ".claude" / "settings.json",
    ),
)
sys.modules["telegram_bot.utils.config"] = config_module

import telegram.error
from telegram import Update

sys.modules.pop("telegram_bot.core.bot", None)
import telegram_bot.core.bot as bot_module
from telegram_bot.core import bot_lifecycle as bot_lifecycle_module

TelegramBot = bot_module.TelegramBot

if _ORIGINAL_PROJECT_ROOT is None:
    os.environ.pop("PROJECT_ROOT", None)
else:
    os.environ["PROJECT_ROOT"] = _ORIGINAL_PROJECT_ROOT

if _ORIGINAL_CONFIG_MODULE is None:
    sys.modules.pop("telegram_bot.utils.config", None)
else:
    sys.modules["telegram_bot.utils.config"] = _ORIGINAL_CONFIG_MODULE

sys.modules.pop("telegram_bot.core.bot", None)

# The manual restore above puts back what this module knows it replaced; the
# guard additionally reverts real modules that were transitively imported
# while the fake config was active (bot_access, bot_lifecycle, ...).
_sys_modules_guard.finish()


class TestConnectionResilience(unittest.TestCase):
    """Test connection resilience and retry logic."""

    def setUp(self):
        """Set up test fixtures."""
        self.bot = TelegramBot(
            settings=config_module.config,
            session_manager=Mock(),
            project_chat=Mock(),
        )

    @patch.object(
        bot_lifecycle_module, "HTTPXRequest", side_effect=lambda **kwargs: kwargs
    )
    def test_builder_configures_timeouts(self, mock_httpx_request):
        """Application.builder() should use dedicated HTTPX requests for polling and API calls."""
        mock_builder = Mock()

        mock_builder.token.return_value = mock_builder
        mock_builder.concurrent_updates.return_value = mock_builder
        mock_builder.get_updates_request.return_value = mock_builder
        mock_builder.request.return_value = mock_builder
        mock_builder.build.return_value = Mock()
        self.bot._application_builder_factory = Mock(return_value=mock_builder)

        self.bot.build()

        self.assertEqual(mock_httpx_request.call_count, 2)
        mock_builder.get_updates_request.assert_called_once()
        polling_request = mock_builder.get_updates_request.call_args.args[0]
        default_request = mock_builder.request.call_args.args[0]

        self.assertEqual(polling_request["connection_pool_size"], 4)
        self.assertEqual(polling_request["read_timeout"], 35.0)
        self.assertEqual(polling_request["pool_timeout"], 5.0)
        self.assertEqual(polling_request["http_version"], "1.1")

        self.assertEqual(default_request["connection_pool_size"], 8)
        self.assertEqual(default_request["read_timeout"], 10.0)
        self.assertEqual(default_request["pool_timeout"], 3.0)
        self.assertEqual(default_request["http_version"], "1.1")

    @patch.dict(
        os.environ,
        {
            "PROXY_URL": "http://proxy.example:8080",
            "https_proxy": "",
            "http_proxy": "",
        },
        clear=False,
    )
    def test_request_builders_use_proxy_and_http11(self):
        """Both request builders should honor proxy settings and force HTTP/1.1."""
        with patch.object(
            bot_lifecycle_module,
            "HTTPXRequest",
            side_effect=lambda **kwargs: kwargs,
        ):
            default_request = self.bot._build_default_request()
            polling_request = self.bot._build_get_updates_request()

        self.assertEqual(default_request["proxy"], "http://proxy.example:8080")
        self.assertEqual(default_request["http_version"], "1.1")
        self.assertEqual(polling_request["proxy"], "http://proxy.example:8080")
        self.assertEqual(polling_request["http_version"], "1.1")

    @patch("telegram_bot.core.bot_lifecycle.shutil.which", return_value="/usr/bin/codex")
    @patch("telegram_bot.core.bot_lifecycle.subprocess.run")
    def test_codex_readiness_uses_codex_login_status(self, mock_run, _mock_which):
        mock_run.return_value = types.SimpleNamespace(
            returncode=0,
            stdout="Logged in using ChatGPT\n",
            stderr="",
        )
        self.bot._config.agent_provider = "codex"
        self.addCleanup(setattr, self.bot._config, "agent_provider", "claude")

        ready, reason = self.bot._probe_agent_readiness()

        self.assertTrue(ready)
        self.assertEqual(reason, "")
        mock_run.assert_called_once_with(
            ["/usr/bin/codex", "login", "status"],
            text=True,
            capture_output=True,
            timeout=15.0,
            check=False,
        )

    def test_invalid_token_raises_system_exit(self):
        """Test that InvalidToken during initialize raises SystemExit."""
        mock_app = Mock()
        mock_app.initialize = AsyncMock(
            side_effect=telegram.error.InvalidToken("bad token")
        )

        self.bot.application = mock_app
        self.bot.build = Mock()
        self.bot._probe_claude_readiness = Mock(return_value=(True, ""))

        with self.assertRaises(SystemExit):
            self.bot.run()

    def test_conflict_raises_system_exit(self):
        """Test that Conflict during initialize raises SystemExit."""
        mock_app = Mock()
        mock_app.initialize = AsyncMock(
            side_effect=telegram.error.Conflict("duplicate")
        )

        self.bot.application = mock_app
        self.bot.build = Mock()
        self.bot._probe_claude_readiness = Mock(return_value=(True, ""))

        with self.assertRaises(SystemExit):
            self.bot.run()

    def test_start_polling_registers_error_callback(self):
        """Polling startup attaches the supervisor error callback."""
        mock_app = Mock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()

        mock_updater = Mock()
        updater_state = {"running": True}
        type(mock_updater).running = PropertyMock(
            side_effect=lambda: updater_state["running"]
        )

        async def stop_updater():
            updater_state["running"] = False

        mock_updater.start_polling = AsyncMock()
        mock_updater.stop = AsyncMock(side_effect=stop_updater)
        mock_app.updater = mock_updater

        type(mock_app).running = PropertyMock(return_value=True)
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()
        mock_app.bot = Mock()

        self.bot.application = mock_app
        self.bot.build = Mock()
        self.bot._on_ready = AsyncMock()
        self.bot._wait_for_polling_exit = AsyncMock(side_effect=SystemExit("stop"))
        self.bot._probe_claude_readiness = Mock(return_value=(True, ""))

        with self.assertRaises(SystemExit):
            self.bot.run()

        _, kwargs = mock_updater.start_polling.call_args
        self.assertEqual(kwargs["allowed_updates"], Update.ALL_TYPES)
        self.assertTrue(kwargs["drop_pending_updates"])
        self.assertEqual(kwargs["error_callback"], self.bot._on_polling_error)

    def test_claude_readiness_probe_does_not_block_event_loop(self):
        """A slow readiness probe must not stall unrelated async work."""
        probe_started = threading.Event()
        release_probe = threading.Event()
        concurrent_progress: list[bool] = []

        def slow_probe():
            probe_started.set()
            if not release_probe.wait(timeout=2.0):
                raise AssertionError("event loop blocked during readiness probe")
            return True, ""

        async def release_from_event_loop():
            while not probe_started.is_set():
                await asyncio.sleep(0)
            concurrent_progress.append(True)
            release_probe.set()

        mock_app = Mock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()

        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)

        async def start_polling(**_kwargs):
            asyncio.create_task(release_from_event_loop())

        mock_updater.start_polling = AsyncMock(side_effect=start_polling)
        mock_updater.stop = AsyncMock()
        mock_app.updater = mock_updater
        type(mock_app).running = PropertyMock(return_value=True)
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()
        mock_app.bot = Mock()

        self.bot.application = mock_app
        self.bot.build = Mock()
        self.bot._on_ready = AsyncMock()
        self.bot._supervise_polling = AsyncMock(side_effect=SystemExit("stop"))
        self.bot._probe_claude_readiness = Mock(side_effect=slow_probe)

        with self.assertRaises(SystemExit):
            self.bot.run()

        self.assertTrue(concurrent_progress)

    def test_signal_stop_queues_shutdown_distill_after_handler_teardown(self):
        """A real process stop records journal work; reconnect teardown does not own it."""
        mock_app = Mock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()
        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)
        mock_updater.start_polling = AsyncMock()
        mock_updater.stop = AsyncMock()
        mock_app.updater = mock_updater
        type(mock_app).running = PropertyMock(return_value=True)
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()
        mock_app.bot = Mock()

        async def stop_polling(stop_event):
            stop_event.set()

        async def assert_handlers_stopped():
            mock_updater.stop.assert_awaited_once_with()
            mock_app.stop.assert_awaited_once_with()
            mock_app.shutdown.assert_awaited_once_with()

        self.bot.application = mock_app
        self.bot.build = Mock()
        self.bot._on_ready = AsyncMock()
        self.bot._probe_agent_readiness = Mock(return_value=(True, ""))
        self.bot._supervise_polling = AsyncMock(side_effect=stop_polling)
        self.bot._enqueue_shutdown_distills = AsyncMock(
            side_effect=assert_handlers_stopped
        )
        self.bot._project_chat.close = AsyncMock()

        asyncio.run(self.bot._run_async())

        self.bot._enqueue_shutdown_distills.assert_awaited_once_with()
        self.bot._project_chat.close.assert_awaited_once_with()

    def test_runtime_conflict_after_polling_start_fails_closed(self):
        """A Conflict emitted by PTB's polling retry loop AFTER polling started
        must still reach the fail-closed SystemExit path (#418 review).

        PTB 22.x retries getUpdates indefinitely with updater.running True and
        its default error callback only logs, so the registered error_callback
        is the only place a post-start Conflict can surface.
        """
        mock_app = Mock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()

        mock_updater = Mock()
        type(mock_updater).running = PropertyMock(return_value=True)
        mock_updater.stop = AsyncMock()

        async def fake_start_polling(**kwargs):
            # Simulate the polling retry loop reporting an asynchronous
            # Conflict shortly after polling started.
            asyncio.get_running_loop().call_later(
                0.05, kwargs["error_callback"], telegram.error.Conflict("duplicate")
            )

        mock_updater.start_polling = AsyncMock(side_effect=fake_start_polling)
        mock_app.updater = mock_updater
        type(mock_app).running = PropertyMock(return_value=True)
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()
        mock_app.bot = Mock()

        self.bot.application = mock_app
        self.bot.build = Mock()
        self.bot._on_ready = AsyncMock()
        self.bot._probe_claude_readiness = Mock(return_value=(True, ""))

        with self.assertRaises(SystemExit) as ctx:
            self.bot.run()

        self.assertIn("Another bot instance", str(ctx.exception))

    @patch("time.time")
    def test_rapid_restart_triggers_system_exit(self, mock_time):
        """Test that repeated rapid polling restarts trigger SystemExit."""
        # Each _run_async iteration: time() at start, time() in _PollingRestart handler
        # Return incrementing values so uptime is always 1s (< MIN_UPTIME=30)
        mock_time.side_effect = list(range(100))

        mock_app = Mock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()

        mock_updater = Mock()
        mock_updater.start_polling = AsyncMock()
        # Polling immediately "exits" to trigger _PollingRestart
        type(mock_updater).running = PropertyMock(return_value=False)
        mock_updater.stop = AsyncMock()
        mock_app.updater = mock_updater
        type(mock_app).running = PropertyMock(return_value=True)
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()
        mock_app.bot = Mock()

        self.bot._on_ready = AsyncMock()
        self.bot._probe_claude_readiness = Mock(return_value=(True, ""))

        build_count = 0

        def mock_build():
            nonlocal build_count
            build_count += 1
            self.bot.application = mock_app

        self.bot.build = mock_build
        self.bot.application = mock_app

        with self.assertRaises(SystemExit) as ctx:
            self.bot.run()

        self.assertIn("Giving up", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
