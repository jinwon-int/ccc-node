"""Regression tests for the Telegram typing-indicator refresh policy.

Bug history: after the agent finished all of its output, the chat could stay
stuck on "typing…" because a refresh fired around response finalization.
The shared guard is ``_should_refresh_typing``; the runtime-path progress loop
(``_agent_progress_loop``, exercised end-to-end in test_project_chat_codex)
consults it before every typing action. These tests pin the guard's
invariants: typing refreshes only while a request is genuinely in flight, and
never after finalization, visible progress, a permission wait, or the
no-progress cap.
"""

import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

# project_chat reads config / PROJECT_ROOT / health / chat_logger at import time.
# Other test modules in this suite replace these in sys.modules with partial
# fakes, so to stay order-independent we install minimal fakes ourselves and
# re-import project_chat fresh (the defensive setup shared by the
# project_chat test modules).
os.environ.setdefault("PROJECT_ROOT", str(Path(__file__).resolve().parents[1]))

from sys_modules_isolation import ModuleFakesGuard  # noqa: E402

_sys_modules_guard = ModuleFakesGuard(__name__).begin()

_config_module = types.ModuleType("telegram_bot.utils.config")
_config_module.config = SimpleNamespace(claude_cli_path=None)
sys.modules["telegram_bot.utils.config"] = _config_module

_chat_logger_module = types.ModuleType("telegram_bot.utils.chat_logger")
_chat_logger_module.log_chat = lambda *args, **kwargs: None
sys.modules["telegram_bot.utils.chat_logger"] = _chat_logger_module

_health_module = types.ModuleType("telegram_bot.utils.health")
_health_module.health_reporter = SimpleNamespace(
    record_claude_error=lambda *args, **kwargs: None,
    record_claude_ok=lambda *args, **kwargs: None,
)
sys.modules["telegram_bot.utils.health"] = _health_module

import importlib  # noqa: E402

# Re-import fresh and reference the SAME module object for both the classes and
# the TYPING_MAX_NO_PROGRESS_SECONDS patch. Using `from telegram_bot.core
# import project_chat` here would grab the parent package's cached attribute
# (the original module a prior test imported), leaving the patch on a different
# object than the handler actually reads.
sys.modules.pop("telegram_bot.core.project_chat", None)
project_chat = importlib.import_module("telegram_bot.core.project_chat")
ProjectChatHandler = project_chat.ProjectChatHandler
_PendingRequest = project_chat._PendingRequest

_sys_modules_guard.finish()


class ShouldRefreshTypingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.handler = ProjectChatHandler()

    def _make_request(self, *, done: bool = False) -> _PendingRequest:
        req = _PendingRequest(
            user_id=1,
            chat_id=2,
            model=None,
            requested_session_id=None,
            permission_callback=None,
            typing_callback=None,
            future=asyncio.get_running_loop().create_future(),
        )
        req.started_at = asyncio.get_running_loop().time()
        if done:
            req.future.set_result("done")
        return req

    async def test_refreshes_typing_while_in_flight(self):
        req = self._make_request()
        now = asyncio.get_running_loop().time()
        self.assertTrue(self.handler._should_refresh_typing(req, now))

    async def test_no_typing_after_future_done(self):
        req = self._make_request(done=True)
        now = asyncio.get_running_loop().time()
        self.assertFalse(
            self.handler._should_refresh_typing(req, now),
            "typing must NOT be refreshed once the response is finalized",
        )

    async def test_typing_stops_after_any_visible_progress(self):
        """Typing stops once the user can see streamed text/tool progress."""
        req = self._make_request()
        now = asyncio.get_running_loop().time()
        req.last_visible_progress_at = now
        self.assertFalse(
            self.handler._should_refresh_typing(req, now),
            "typing must NOT be refreshed after visible progress has appeared",
        )

    async def test_typing_skips_permission_wait(self):
        """Permission prompts wait for the user; typing must not imply work."""
        req = self._make_request()
        req.awaiting_permission = True
        now = asyncio.get_running_loop().time()
        self.assertFalse(self.handler._should_refresh_typing(req, now))

    async def test_typing_stops_after_no_progress_cap(self):
        req = self._make_request()
        orig_cap = project_chat.TYPING_MAX_NO_PROGRESS_SECONDS
        setattr(project_chat, "TYPING_MAX_NO_PROGRESS_SECONDS", 0.02)
        self.addCleanup(
            setattr, project_chat, "TYPING_MAX_NO_PROGRESS_SECONDS", orig_cap
        )
        now = asyncio.get_running_loop().time()
        req.started_at = now - 1.0
        self.assertFalse(self.handler._should_refresh_typing(req, now))

    def test_invalid_typing_cap_env_falls_back(self):
        self.assertEqual(project_chat._env_int("MISSING_CCC_TEST_INT", 600), 600)
        with self.assertLogs(project_chat.logger, level="WARNING"):
            self.assertEqual(project_chat._env_int("PATH", 600), 600)


if __name__ == "__main__":
    unittest.main()
