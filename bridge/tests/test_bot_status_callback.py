"""Behavior tests for the BotStatusMixin heartbeat callback (#348).

The callback is the bridge's only writer of "⏳ Working" status messages, and
it must fail open: a Telegram error can never propagate into the request flow,
and the heartbeat registry must reflect exactly which messages still exist so
the startup sweep can delete frozen ones. Covers the send/edit/delete/error
replacement/error paths against the real heartbeat store in a temp directory.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import telegram.error

from telegram_bot.core.bot_status import BotStatusMixin
from telegram_bot.utils.heartbeat_store import drain_heartbeats, store_path_for


class _FakeBot:
    def __init__(self, *, edit_error: Optional[Exception] = None,
                 delete_error: Optional[Exception] = None,
                 send_error: Optional[Exception] = None,
                 sent_message_id=777):
        self.sent = []
        self.edited = []
        self.deleted = []
        self._edit_error = edit_error
        self._send_error = send_error
        self.silent = []
        self._delete_error = delete_error
        self._sent_message_id = sent_message_id

    async def send_message(self, chat_id, text, disable_notification=False):
        if self._send_error is not None:
            raise self._send_error
        self.sent.append((chat_id, text))
        self.silent.append(disable_notification)
        value = self._sent_message_id
        if type(value) is int:
            self._sent_message_id += 1
        return SimpleNamespace(message_id=value)

    async def edit_message_text(self, chat_id, message_id, text):
        if self._edit_error is not None:
            raise self._edit_error
        self.edited.append((chat_id, message_id, text))

    async def delete_message(self, chat_id, message_id):
        if self._delete_error is not None:
            raise self._delete_error
        self.deleted.append((chat_id, message_id))


class StatusHarness(BotStatusMixin):
    def __init__(self, bot_data_dir, *, delete_on_done: bool = True):
        self._config = SimpleNamespace(
            bot_data_dir=bot_data_dir,
            heartbeat_store_path=None,
            heartbeat_delete_on_done=delete_on_done,
        )


class StatusCallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def _store_refs(self):
        path = store_path_for(self.tmpdir, None)
        return drain_heartbeats(path)

    def test_send_returns_message_id_and_registers_heartbeat(self):
        bot = _FakeBot(sent_message_id=555)
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

        result = asyncio.run(callback("⏳ Working"))

        self.assertEqual(result, 555)
        self.assertEqual(bot.sent, [(42, "⏳ Working")])
        self.assertEqual(self._store_refs(), [(42, 555)])

    def test_send_with_non_integer_message_id_returns_none_and_skips_registry(self):
        bot = _FakeBot(sent_message_id=None)
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

        self.assertIsNone(asyncio.run(callback("⏳ Working")))
        self.assertEqual(self._store_refs(), [])

    def test_send_without_store_path_still_delivers(self):
        bot = _FakeBot(sent_message_id=9)
        harness = StatusHarness(None)
        callback = harness._make_status_callback(bot, chat_id=42)

        self.assertEqual(asyncio.run(callback("⏳ Working")), 9)
        self.assertEqual(len(bot.sent), 1)

    def test_update_replaces_message_after_intervening_chat_message(self):
        bot = _FakeBot(sent_message_id=555)
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

        async def scenario():
            old = await callback("⏳ Working")
            await bot.send_message(42, "User-visible progress")
            return await callback("⏳ Waiting for progress", old)

        self.assertEqual(asyncio.run(scenario()), 557)
        self.assertEqual(bot.sent[-1], (42, "⏳ Waiting for progress"))
        self.assertEqual(bot.deleted, [(42, 555)])
        self.assertEqual(bot.edited, [])
        self.assertEqual(bot.silent, [True, False, True])
        self.assertEqual(self._store_refs(), [(42, 557)])

    def test_failed_replacement_keeps_old_message_and_registry(self):
        bot = _FakeBot(sent_message_id=555)
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

        async def scenario():
            old = await callback("⏳ Working")
            bot._send_error = telegram.error.RetryAfter(30)
            return await callback("update", old)

        self.assertEqual(asyncio.run(scenario()), 555)
        self.assertEqual(bot.deleted, [])
        self.assertEqual(self._store_refs(), [(42, 555)])

    def test_failed_old_delete_is_bounded_and_retried_before_replacement(self):
        bot = _FakeBot(sent_message_id=555)
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

        async def scenario():
            old = await callback("first")
            bot._delete_error = telegram.error.TelegramError("unavailable")
            current = await callback("second", old)
            self.assertEqual(current, 556)
            for _ in range(5):
                self.assertEqual(await callback("retry", current), 556)
            self.assertEqual(len(bot.sent), 2)
            bot._delete_error = None
            return await callback("third", current)

        self.assertEqual(asyncio.run(scenario()), 557)
        self.assertEqual(bot.deleted, [(42, 555), (42, 556)])
        self.assertEqual(self._store_refs(), [(42, 557)])

    def test_cleanup_retries_stale_predecessor_and_current_message(self):
        bot = _FakeBot(sent_message_id=555)
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

        async def scenario():
            old = await callback("first")
            bot._delete_error = telegram.error.TelegramError("unavailable")
            current = await callback("second", old)
            bot._delete_error = None
            return await callback(None, current)

        self.assertIsNone(asyncio.run(scenario()))
        self.assertEqual(bot.deleted, [(42, 555), (42, 556)])
        self.assertEqual(self._store_refs(), [])

    def test_already_deleted_message_does_not_block_refresh(self):
        bot = _FakeBot(delete_error=telegram.error.BadRequest("Message to delete not found"))
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)
        self.assertEqual(asyncio.run(callback("update", 7)), 777)
        self.assertEqual(self._store_refs(), [(42, 777)])

    def test_cancel_during_old_delete_retains_new_id_for_cleanup(self):
        bot = _FakeBot(sent_message_id=555, delete_error=asyncio.CancelledError())
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

        async def scenario():
            with self.assertRaises(asyncio.CancelledError):
                await callback("replacement", 7)
            bot._delete_error = None
            return await callback(None, 7)

        self.assertIsNone(asyncio.run(scenario()))
        self.assertEqual(bot.deleted, [(42, 7), (42, 555)])
        self.assertEqual(self._store_refs(), [])

    def test_delete_removes_message_and_registry_entry(self):
        bot = _FakeBot(sent_message_id=555)
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

        async def scenario():
            message_id = await callback("⏳ Working")
            return await callback(None, message_id=message_id)

        self.assertIsNone(asyncio.run(scenario()))
        self.assertEqual(bot.deleted, [(42, 555)])
        self.assertEqual(self._store_refs(), [])

    def test_delete_disabled_keeps_message_but_unregisters_it(self):
        bot = _FakeBot(sent_message_id=555)
        harness = StatusHarness(self.tmpdir, delete_on_done=False)
        callback = harness._make_status_callback(bot, chat_id=42)

        async def scenario():
            message_id = await callback("⏳ Working")
            return await callback(None, message_id=message_id)

        self.assertIsNone(asyncio.run(scenario()))
        self.assertEqual(bot.deleted, [])
        self.assertEqual(self._store_refs(), [])

    def test_failed_delete_keeps_heartbeat_registered_for_next_sweep(self):
        bot = _FakeBot(sent_message_id=555,
                       delete_error=telegram.error.TelegramError("boom"))
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

        async def scenario():
            message_id = await callback("⏳ Working")
            return await callback(None, message_id=message_id)

        # Fail-open: the callback reports the message as still present.
        self.assertEqual(asyncio.run(scenario()), 555)
        self.assertEqual(self._store_refs(), [(42, 555)])


if __name__ == "__main__":
    unittest.main()
