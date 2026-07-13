"""Behavior tests for the BotStatusMixin heartbeat callback (#348).

The callback is the bridge's only writer of "⏳ Working" status messages, and
it must fail open: a Telegram error can never propagate into the request flow,
and the heartbeat registry must reflect exactly which messages still exist so
the startup sweep can delete frozen ones. Covers the send/edit/delete/error
paths against the real heartbeat store in a temp directory.
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
                 sent_message_id=777):
        self.sent = []
        self.edited = []
        self.deleted = []
        self._edit_error = edit_error
        self._delete_error = delete_error
        self._sent_message_id = sent_message_id

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=self._sent_message_id)

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

    def test_edit_returns_same_message_id(self):
        bot = _FakeBot()
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

        self.assertEqual(asyncio.run(callback("still working", message_id=7)), 7)
        self.assertEqual(bot.edited, [(42, 7, "still working")])

    def test_edit_not_modified_error_is_swallowed(self):
        bot = _FakeBot(edit_error=telegram.error.BadRequest("Message is not modified"))
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

        self.assertEqual(asyncio.run(callback("same text", message_id=7)), 7)

    def test_edit_other_bad_request_fails_open_with_warning(self):
        bot = _FakeBot(edit_error=telegram.error.BadRequest("Chat not found"))
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

        with self.assertLogs("telegram_bot.core.bot_status", level="WARNING") as logs:
            result = asyncio.run(callback("update", message_id=7))

        self.assertEqual(result, 7)
        self.assertTrue(any("Heartbeat status callback failed" in m for m in logs.output))

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
