"""Regression tests for the Telegram typing-indicator lifecycle.

Bug: after the agent finished all of its output, the chat could stay stuck on
"typing…". Root cause: the typing keepalive loop refreshed the indicator off the
head of state.pending and would fire one more typing action around the moment a
request's response was finalized (future resolved) but before it was popped.
Streamed replies edit drafts instead of sending a new message, so they never
clear typing on their own — a stray keepalive left it stuck.

Fix: the keepalive (and the reader-loop refresh) skip a request whose future is
already done. These tests pin that: typing is refreshed while genuinely in
flight, and never after the response is finalized.
"""

import asyncio
import os
import sys
import types
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace

# project_chat reads config / PROJECT_ROOT / health / chat_logger at import time.
# Other test modules in this suite replace these in sys.modules with partial
# fakes, so to stay order-independent we install minimal fakes ourselves and
# re-import project_chat fresh (the defensive setup used by
# test_project_chat_retry).
os.environ.setdefault("PROJECT_ROOT", str(Path(__file__).resolve().parents[1]))

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
# the TYPING_INTERVAL patch. Using `from telegram_bot.core import project_chat`
# here would grab the parent package's cached attribute (the original module a
# prior test imported), leaving the patch on a different object than the handler
# actually reads.
sys.modules.pop("telegram_bot.core.project_chat", None)
project_chat = importlib.import_module("telegram_bot.core.project_chat")
ProjectChatHandler = project_chat.ProjectChatHandler
_PendingRequest = project_chat._PendingRequest
_UserStreamState = project_chat._UserStreamState


class TypingKeepaliveTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Shrink the interval so the loop iterates quickly under test.
        self._orig_interval = project_chat.TYPING_INTERVAL
        project_chat.TYPING_INTERVAL = 0.01
        self.addCleanup(setattr, project_chat, "TYPING_INTERVAL", self._orig_interval)
        self.handler = ProjectChatHandler()
        self.calls = 0
        self.called = asyncio.Event()

    def _make_request(self, *, done: bool) -> _PendingRequest:
        async def typing_callback():
            self.calls += 1
            self.called.set()

        req = _PendingRequest(
            user_id=1,
            chat_id=2,
            model=None,
            requested_session_id=None,
            permission_callback=None,
            typing_callback=typing_callback,
            future=asyncio.get_running_loop().create_future(),
        )
        if done:
            req.future.set_result("done")
        return req

    async def _start_loop(self, state):
        task = asyncio.create_task(self.handler._typing_keepalive_loop(1, state))
        self.addCleanup(self._cancel, task)
        return task

    @staticmethod
    async def _cancel(task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_refreshes_typing_while_in_flight(self):
        req = self._make_request(done=False)  # still working
        state = _UserStreamState(client=None, model=None, pending=deque([req]))
        await self._start_loop(state)
        # Deterministic: wait until typing is actually sent (or fail on timeout).
        await asyncio.wait_for(self.called.wait(), timeout=2.0)
        self.assertGreater(self.calls, 0)

    async def test_no_typing_after_future_done(self):
        req = self._make_request(done=True)  # response finalized
        state = _UserStreamState(client=None, model=None, pending=deque([req]))
        await self._start_loop(state)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.called.wait(), timeout=0.2)
        self.assertEqual(self.calls, 0, "typing must NOT be sent once the response is finalized")

    async def test_stops_when_future_resolves_mid_run(self):
        req = self._make_request(done=False)
        state = _UserStreamState(client=None, model=None, pending=deque([req]))
        await self._start_loop(state)
        await asyncio.wait_for(self.called.wait(), timeout=2.0)  # at least one typing
        # Finalize the response, then assert no further typing is sent.
        req.future.set_result("done")
        count_at_finalize = self.calls
        await asyncio.sleep(0.1)  # several keepalive intervals (0.01 each)
        self.assertEqual(self.calls, count_at_finalize, "typing kept firing after finalize")

    async def test_no_typing_when_pending_empty(self):
        state = _UserStreamState(client=None, model=None, pending=deque())
        await self._start_loop(state)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.called.wait(), timeout=0.2)
        self.assertEqual(self.calls, 0)


if __name__ == "__main__":
    unittest.main()
