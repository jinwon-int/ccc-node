"""Regression test: /revert must cancel the in-flight task of the SAME conversation.

Bug: the run queue is keyed per conversation (``user_id:chat_id`` in groups), but
``_cancel_active_operations`` looked the active task up by the bare ``user_id``
only. In a group chat the in-flight task is stored under ``user_id:chat_id`` and
was therefore never cancelled — revert truncated the conversation JSONL while the
task kept running. Fixed by passing chat_id through and using the conversation
key (with a user_id fallback for DMs), matching /stop.
"""

# ruff: noqa: E402
import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

# config.py reads PROJECT_ROOT + a bot token at import time.
os.environ.setdefault("PROJECT_ROOT", str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test")

# Other test modules inject partial fakes for these into sys.modules (e.g. a
# chat_logger without log_debug). Drop any such fakes so bot imports the real
# modules here regardless of collection order, then RESTORE the prior sys.modules
# state so this import doesn't perturb modules collected after us.
_VOLATILE = (
    "telegram_bot.utils.config",
    "telegram_bot.utils.chat_logger",
    "telegram_bot.utils.health",
    "telegram_bot.core.project_chat",
    "telegram_bot.core.bot",
)
_snapshot = {name: sys.modules.get(name) for name in _VOLATILE}
for _m in _VOLATILE:
    sys.modules.pop(_m, None)
try:
    from telegram_bot.core.bot import TelegramBot
    from telegram_bot.core.task_queue import UserTaskQueue
finally:
    for _name, _mod in _snapshot.items():
        if _mod is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _mod


class RevertCancelScopeTests(unittest.IsolatedAsyncioTestCase):
    def _bare_bot(self):
        # Build just enough of a TelegramBot to exercise _cancel_active_operations
        # without running __init__ (which touches config-derived dirs).
        bot = TelegramBot.__new__(TelegramBot)
        bot._tasks = UserTaskQueue()
        bot._user_voice_tasks = {}
        bot._cancel_user_streaming = AsyncMock(return_value=True)
        return bot

    async def _pending_task(self):
        task = asyncio.create_task(asyncio.sleep(30))
        await asyncio.sleep(0)  # let it start
        self.addCleanup(task.cancel)
        return task

    async def test_group_chat_active_task_cancelled(self):
        bot = self._bare_bot()
        user_id, chat_id = 7, 999  # distinct chat -> conversation key "7:999"
        key = bot._conversation_key(user_id, chat_id)
        self.assertEqual(key, "7:999")
        task = await self._pending_task()
        bot._tasks._active[key] = task

        await bot._cancel_active_operations(user_id, chat_id)

        self.assertTrue(task.cancelled() or task.done())
        bot._cancel_user_streaming.assert_awaited_once_with(user_id, chat_id)

    async def test_dm_active_task_still_cancelled(self):
        bot = self._bare_bot()
        user_id, chat_id = 7, 7  # DM -> conversation key == user_id
        key = bot._conversation_key(user_id, chat_id)
        self.assertEqual(key, user_id)
        task = await self._pending_task()
        bot._tasks._active[key] = task

        await bot._cancel_active_operations(user_id, chat_id)

        self.assertTrue(task.cancelled() or task.done())

    async def test_legacy_user_id_key_fallback(self):
        # A task stored under the bare user_id (legacy/DM) is still cancelled even
        # when a chat_id is supplied, via the fallback.
        bot = self._bare_bot()
        user_id, chat_id = 7, 999
        task = await self._pending_task()
        bot._tasks._active[user_id] = task

        await bot._cancel_active_operations(user_id, chat_id)

        self.assertTrue(task.cancelled() or task.done())


class VoiceCancelScopeTests(unittest.IsolatedAsyncioTestCase):
    """/stop and /new cancel voice transcription per conversation, not globally.

    Voice tasks used to be tracked by bare user_id, so /stop in one chat cancelled
    the same user's voice work in every other chat. They are now keyed by the
    conversation key.
    """

    def _bare_bot(self):
        bot = TelegramBot.__new__(TelegramBot)
        bot._user_voice_tasks = {}
        return bot

    async def _pending_task(self):
        task = asyncio.create_task(asyncio.sleep(30))
        await asyncio.sleep(0)
        self.addCleanup(task.cancel)
        return task

    async def test_voice_cancel_does_not_cross_chats(self):
        bot = self._bare_bot()
        key_a = bot._conversation_key(7, 100)  # "7:100"
        key_b = bot._conversation_key(7, 200)  # "7:200"
        task_a = await self._pending_task()
        bot._track_voice_task(key_a, task_a)

        # /stop in chat B must not touch chat A's voice task.
        cancelled = await bot._cancel_user_voice_tasks(key_b)
        self.assertEqual(cancelled, 0)
        self.assertFalse(task_a.cancelled())

        # /stop in chat A cancels it.
        cancelled = await bot._cancel_user_voice_tasks(key_a)
        self.assertEqual(cancelled, 1)
        self.assertTrue(task_a.cancelled() or task_a.done())


if __name__ == "__main__":
    unittest.main()
