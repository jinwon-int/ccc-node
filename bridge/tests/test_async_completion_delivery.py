"""Tests for the durable async-completion delivery path (#646 slice 2)."""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from telegram_bot.core.async_completion_delivery import (
    MAX_COMPLETION_TEXT_BYTES,
    AsyncCompletionDeliveryCoordinator,
    bounded_completion_text,
    format_completion_notice,
)
from telegram_bot.core.async_completion_event import (
    NormalizedAsyncCompletionEvent,
)
from telegram_bot.core.async_completion_journal import AsyncCompletionJournal


def _event(turn_id: str = "turn-1111", generation: int = 4):
    return NormalizedAsyncCompletionEvent(
        provider="codex",
        thread_id="thread-aaaa",
        conversation_route_id="7",
        session_generation=generation,
        turn_id=turn_id,
    )


def _make_journal(root: Path) -> AsyncCompletionJournal:
    journal = AsyncCompletionJournal(root / "async-completions")
    journal.initialize()
    return journal


def _coordinator(journal, *, sender, generations=None, max_attempts=3, backoff=0.0):
    registry = dict(generations or {})
    return AsyncCompletionDeliveryCoordinator(
        journal,
        lock_factory=lambda user_id, chat_id: asyncio.Lock(),
        sender=sender,
        generation_probe=lambda user_id, chat_id: registry.get((user_id, chat_id), 0),
        max_attempts=max_attempts,
        attempt_backoff_seconds=backoff,
        lock_timeout_seconds=0.5,
        send_timeout_seconds=1.0,
    )


class TestBoundedCompletionText(unittest.TestCase):
    def test_extracts_agent_message_chunks(self):
        items = [
            {"type": "userMessage", "text": "ignored"},
            {"type": "agentMessage", "text": "first"},
            {"id": "x", "type": "agentMessage", "text": "second"},
        ]
        self.assertEqual(bounded_completion_text(items), "first\nsecond")

    def test_non_list_and_foreign_shapes_yield_none(self):
        self.assertIsNone(bounded_completion_text(None))
        self.assertIsNone(bounded_completion_text("text"))
        self.assertIsNone(bounded_completion_text([{"type": "agentMessage"}]))
        self.assertIsNone(bounded_completion_text([{"type": "agentMessage", "text": 5}]))
        self.assertIsNone(bounded_completion_text([]))

    def test_oversized_body_is_bounded(self):
        text = "x" * (MAX_COMPLETION_TEXT_BYTES + 1000)
        extracted = bounded_completion_text([{"type": "agentMessage", "text": text}])
        assert extracted is not None
        self.assertLessEqual(len(extracted.encode("utf-8")), MAX_COMPLETION_TEXT_BYTES)

    def test_multibyte_truncation_is_valid_utf8(self):
        text = "가" * 2000  # 3 bytes each: 6000 bytes > bound
        extracted = bounded_completion_text([{"type": "agentMessage", "text": text}])
        assert extracted is not None
        encoded = extracted.encode("utf-8")
        self.assertLessEqual(len(encoded), MAX_COMPLETION_TEXT_BYTES)
        self.assertTrue(extracted.endswith("가"))


class TestFormatCompletionNotice(unittest.TestCase):
    def test_with_body_includes_text(self):
        notice = format_completion_notice("result")
        self.assertIn("Background task completed", notice)
        self.assertIn("result", notice)

    def test_without_body_stays_body_free(self):
        notice = format_completion_notice(None, completion_count=2)
        self.assertNotIn("result", notice)
        self.assertIn("x2", notice)


class TestCoordinatorDelivery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.journal = _make_journal(Path(self._tmp.name))

    async def test_happy_path_claims_sends_and_marks_delivered(self):
        event = _event()
        self.journal.observe(event, deliverable=True)
        sent = []

        async def sender(user_id, chat_id, text):
            sent.append((user_id, chat_id, text))
            return True

        coordinator = _coordinator(
            self.journal, sender=sender, generations={(7, 7): 4}
        )
        delivered = await coordinator.deliver(
            event.idempotency_key,
            user_id=7,
            chat_id=7,
            session_generation=4,
            text="hello",
        )
        self.assertTrue(delivered)
        self.assertEqual(sent, [(7, 7, format_completion_notice("hello"))])
        record = self.journal.get(event.idempotency_key)
        assert record is not None
        self.assertEqual(record.state, "delivered")

    async def test_duplicate_observation_cannot_send_twice(self):
        event = _event()
        self.journal.observe(event, deliverable=True)
        sends = 0

        async def sender(user_id, chat_id, text):
            nonlocal sends
            sends += 1
            return True

        coordinator = _coordinator(
            self.journal, sender=sender, generations={(7, 7): 4}
        )
        first = await coordinator.deliver(
            event.idempotency_key,
            user_id=7,
            chat_id=7,
            session_generation=4,
            text=None,
        )
        # A second coordinator (concurrent duplicate) runs against the same
        # already-claimed record: the claim race fails closed, no send.
        second = await coordinator.deliver(
            event.idempotency_key,
            user_id=7,
            chat_id=7,
            session_generation=4,
            text=None,
        )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(sends, 1)

    async def test_generation_mismatch_terminals_without_sending(self):
        event = _event(generation=4)
        self.journal.observe(event, deliverable=True)
        sends = 0

        async def sender(user_id, chat_id, text):
            nonlocal sends
            sends += 1
            return True

        coordinator = _coordinator(
            self.journal, sender=sender, generations={(7, 7): 9}
        )
        delivered = await coordinator.deliver(
            event.idempotency_key,
            user_id=7,
            chat_id=7,
            session_generation=4,
            text="stale",
        )
        self.assertFalse(delivered)
        self.assertEqual(sends, 0)
        record = self.journal.get(event.idempotency_key)
        assert record is not None
        self.assertEqual(record.state, "terminal_failed")
        self.assertEqual(record.last_error_code, "generation_mismatch")

    async def test_send_failures_exhaust_to_terminal(self):
        event = _event()
        self.journal.observe(event, deliverable=True)
        attempts = 0

        async def sender(user_id, chat_id, text):
            nonlocal attempts
            attempts += 1
            return False

        coordinator = _coordinator(
            self.journal, sender=sender, generations={(7, 7): 4}, max_attempts=3
        )
        delivered = await coordinator.deliver(
            event.idempotency_key,
            user_id=7,
            chat_id=7,
            session_generation=4,
            text=None,
        )
        self.assertFalse(delivered)
        self.assertEqual(attempts, 3)
        record = self.journal.get(event.idempotency_key)
        assert record is not None
        self.assertEqual(record.state, "terminal_failed")
        self.assertEqual(record.last_error_code, "send_failed")

    async def test_sender_exception_counts_as_failed_attempt(self):
        event = _event()
        self.journal.observe(event, deliverable=True)

        async def sender(user_id, chat_id, text):
            raise RuntimeError("telegram exploded")

        coordinator = _coordinator(
            self.journal,
            sender=sender,
            generations={(7, 7): 4},
            max_attempts=2,
        )
        delivered = await coordinator.deliver(
            event.idempotency_key,
            user_id=7,
            chat_id=7,
            session_generation=4,
            text=None,
        )
        self.assertFalse(delivered)
        record = self.journal.get(event.idempotency_key)
        assert record is not None
        self.assertEqual(record.state, "terminal_failed")

    async def test_lock_timeout_retries_then_terminals(self):
        event = _event()
        self.journal.observe(event, deliverable=True)
        sends = 0

        async def sender(user_id, chat_id, text):
            nonlocal sends
            sends += 1
            return True

        stuck_lock = asyncio.Lock()
        await stuck_lock.acquire()

        coordinator = AsyncCompletionDeliveryCoordinator(
            self.journal,
            lock_factory=lambda user_id, chat_id: stuck_lock,
            sender=sender,
            generation_probe=lambda user_id, chat_id: 4,
            max_attempts=2,
            attempt_backoff_seconds=0.0,
            lock_timeout_seconds=0.05,
            send_timeout_seconds=1.0,
        )
        delivered = await coordinator.deliver(
            event.idempotency_key,
            user_id=7,
            chat_id=7,
            session_generation=4,
            text=None,
        )
        self.assertFalse(delivered)
        self.assertEqual(sends, 0)
        record = self.journal.get(event.idempotency_key)
        assert record is not None
        self.assertEqual(record.state, "terminal_failed")
        self.assertEqual(record.last_error_code, "lock_timeout")

    async def test_retryable_then_delivered(self):
        event = _event()
        self.journal.observe(event, deliverable=True)
        results = iter([False, True])
        sends = 0

        async def sender(user_id, chat_id, text):
            nonlocal sends
            sends += 1
            return next(results)

        coordinator = _coordinator(
            self.journal,
            sender=sender,
            generations={(7, 7): 4},
            max_attempts=3,
            backoff=0.0,
        )
        delivered = await coordinator.deliver(
            event.idempotency_key,
            user_id=7,
            chat_id=7,
            session_generation=4,
            text=None,
        )
        self.assertTrue(delivered)
        self.assertEqual(sends, 2)
        record = self.journal.get(event.idempotency_key)
        assert record is not None
        self.assertEqual(record.state, "delivered")
        # Retries happen inside one claim window: attempts counts claims.
        self.assertEqual(record.attempts, 1)

    def test_invalid_configuration_is_rejected(self):
        async def sender(user_id, chat_id, text):
            return True

        with self.assertRaises(ValueError):
            AsyncCompletionDeliveryCoordinator(
                self.journal,
                lock_factory=lambda user_id, chat_id: asyncio.Lock(),
                sender=sender,
                generation_probe=lambda user_id, chat_id: 0,
                max_attempts=0,
            )


class TestTelegramSender(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_sender_success_and_failure(self):
        from telegram_bot.core.async_completion_delivery import (
            build_telegram_sender,
        )

        class _FakeBot:
            def __init__(self, fail):
                self.fail = fail
                self.calls = []

            async def send_message(self, *, chat_id, text):
                self.calls.append((chat_id, text))
                if self.fail:
                    raise RuntimeError("network down")

        ok_bot = _FakeBot(fail=False)
        sender = build_telegram_sender(ok_bot, send_timeout=1.0)
        assert await sender(7, 42, "hello") is True
        assert ok_bot.calls == [(42, "hello")]

        bad_bot = _FakeBot(fail=True)
        assert await build_telegram_sender(bad_bot, send_timeout=1.0)(7, 42, "x") is False


if __name__ == "__main__":
    unittest.main()
