"""Behavior tests for the BotStatusMixin heartbeat callback (#348).

The callback is the bridge's only writer of "⏳ Working" status messages, and
it must fail open: a Telegram error can never propagate into the request flow,
and the heartbeat registry must reflect exactly which messages still exist so
the startup sweep can delete frozen ones. Covers send-once updates, deletion, cancellation,
and retry paths against the real heartbeat store in a temp directory.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

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

    def test_update_edits_original_after_intervening_chat_message(self):
        bot = _FakeBot(sent_message_id=555)
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

        async def scenario():
            old = await callback("⏳ Working")
            await bot.send_message(42, "User-visible progress")
            return await callback("⏳ Waiting for progress", old)

        self.assertEqual(asyncio.run(scenario()), 555)
        self.assertEqual(bot.sent, [(42, "⏳ Working"), (42, "User-visible progress")])
        self.assertEqual(bot.deleted, [])
        self.assertEqual(bot.edited, [(42, 555, "⏳ Waiting for progress")])
        self.assertEqual(bot.silent, [True, False])
        self.assertEqual(self._store_refs(), [(42, 555)])

    def test_edit_transport_failure_retries_same_id_without_sending(self):
        bot = _FakeBot(sent_message_id=555)
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

        async def scenario():
            old = await callback("first")
            bot._edit_error = telegram.error.TimedOut("unavailable")
            for _ in range(5):
                self.assertEqual(await callback("second", old), 555)
            bot._edit_error = None
            return await callback("third", old)

        self.assertEqual(asyncio.run(scenario()), 555)
        self.assertEqual(bot.sent, [(42, "first")])
        self.assertEqual(bot.edited, [(42, 555, "third")])
        self.assertEqual(bot.deleted, [])
        self.assertEqual(self._store_refs(), [(42, 555)])

    def test_unchanged_edit_is_success_without_resend(self):
        bot = _FakeBot(edit_error=telegram.error.BadRequest("Message is not modified"))
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)
        self.assertEqual(asyncio.run(callback("same", 7)), 7)
        self.assertEqual(bot.sent, [])
        self.assertEqual(bot.deleted, [])

    def test_edit_permission_error_never_falls_back_to_send(self):
        bot = _FakeBot(edit_error=telegram.error.BadRequest("Message can't be edited"))
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)
        self.assertEqual(asyncio.run(callback("update", 7)), 7)
        self.assertEqual(bot.sent, [])

    def test_record_failure_still_retains_id_for_edit_and_cleanup(self):
        bot = _FakeBot(sent_message_id=555)
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

        async def scenario():
            with patch("telegram_bot.core.bot_status.record_heartbeat", side_effect=OSError):
                self.assertEqual(await callback("first"), 555)
            self.assertEqual(await callback("second"), 555)
            await callback(None)

        asyncio.run(scenario())
        self.assertEqual(bot.sent, [(42, "first")])
        self.assertEqual(bot.edited, [(42, 555, "second")])
        self.assertEqual(bot.deleted, [(42, 555)])

    def test_refresh_recovers_registry_after_initial_storage_outage(self):
        for unchanged in [False, True]:
            with self.subTest(unchanged=unchanged):
                bot = _FakeBot(sent_message_id=555)
                callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

                async def scenario():
                    with patch("telegram_bot.utils.heartbeat_store._write", side_effect=OSError):
                        mid = await callback("first")
                    if unchanged:
                        bot._edit_error = telegram.error.BadRequest("Message is not modified")
                    return await callback("next", mid)

                self.assertEqual(asyncio.run(scenario()), 555)
                self.assertEqual(len(bot.sent), 1)
                self.assertEqual(self._store_refs(), [(42, 555)])

    def test_failed_delete_never_creates_a_second_cleanup_obligation(self):
        bot = _FakeBot(sent_message_id=555)
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

        async def scenario():
            old = await callback("first")
            bot._delete_error = telegram.error.TelegramError("unavailable")
            for _ in range(5):
                self.assertEqual(await callback("replacement", old), 555)
            self.assertEqual(await callback(None, old), 555)
            self.assertEqual(len(bot.sent), 1)
            # Terminal cleanup still owns exactly the original ID.
            bot._delete_error = None
            return await callback(None, old)

        self.assertIsNone(asyncio.run(scenario()))
        self.assertEqual(bot.deleted, [(42, 555)])
        self.assertEqual(self._store_refs(), [])

    def test_deleted_message_is_not_recreated_even_with_stale_caller_id(self):
        bot = _FakeBot(sent_message_id=555)
        callback = StatusHarness(self.tmpdir)._make_status_callback(bot, chat_id=42)

        async def scenario():
            old = await callback("first")
            bot._edit_error = telegram.error.BadRequest("Message to edit not found")
            self.assertIsNone(await callback("second", old))
            for caller_id in [None, old, None]:
                self.assertIsNone(await callback("third", caller_id))
            await callback(None)

        asyncio.run(scenario())
        self.assertEqual(bot.sent, [(42, "first")])
        self.assertEqual(self._store_refs(), [])

    def test_edit_updates_do_not_cross_chat_or_task_boundaries(self):
        bot = _FakeBot(sent_message_id=555)
        harness = StatusHarness(self.tmpdir)
        callbacks = [harness._make_status_callback(bot, chat_id=c) for c in [42, 43, 42]]

        async def scenario():
            ids = await asyncio.gather(*(cb("first") for cb in callbacks))
            for _ in range(3):
                self.assertEqual(await asyncio.gather(*(cb("next", mid) for cb, mid in zip(callbacks, ids))), ids)
            await asyncio.gather(*(cb(None, mid) for cb, mid in zip(callbacks, ids)))

        asyncio.run(scenario())
        self.assertEqual(len(bot.sent), 3)
        self.assertEqual({(c, m) for c, m, _ in bot.edited}, {(42, 555), (43, 556), (42, 557)})
        self.assertEqual(len(bot.deleted), 3)
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
