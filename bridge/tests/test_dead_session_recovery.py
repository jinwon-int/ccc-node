import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram_bot.core.dead_session_recovery import (
    MARKER_KEY,
    TranscriptRejected,
    format_notification,
    notification_marker,
    parse_conversation_route,
    recover_dead_session_notifications,
    scan_transcript,
)


def _queue(operation, session_id="session-1", content=None):
    row = {
        "type": "queue-operation",
        "operation": operation,
        "sessionId": session_id,
        "timestamp": "2026-07-11T00:00:00Z",
    }
    if content is not None:
        row["content"] = content
    return json.dumps(row) + "\n"


def _task(task_id="task-1", status="completed", summary="done"):
    return (
        "<task-notification>"
        f"<task-id>{task_id}</task-id>"
        f"<status>{status}</status>"
        f"<summary>{summary}</summary>"
        "</task-notification>"
    )


class TranscriptRecoveryParserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "session-1.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, *lines):
        self.path.write_text("".join(lines), encoding="utf-8")

    def test_replays_fifo_and_remove_consumes_oldest(self):
        self._write(
            _queue("enqueue", content="ordinary queued user text"),
            _queue("enqueue", content=_task()),
            _queue("remove"),
        )
        found = scan_transcript(self.path, "session-1")
        self.assertEqual([(item.task_id, item.status, item.summary) for item in found], [("task-1", "completed", "done")])

    def test_dequeued_terminal_notification_is_not_recovered(self):
        self._write(
            _queue("enqueue", content=_task()),
            _queue("dequeue"),
        )
        self.assertEqual(scan_transcript(self.path, "session-1"), [])

    def test_statusless_event_and_non_task_content_are_ignored(self):
        event = (
            "<task-notification><task-id>task-1</task-id>"
            "<summary>progress only</summary><event>arbitrary output</event>"
            "</task-notification>"
        )
        self._write(
            _queue("enqueue", content="ordinary text"),
            _queue("enqueue", content=event),
        )
        self.assertEqual(scan_transcript(self.path, "session-1"), [])

    def test_failed_notification_is_terminal(self):
        self._write(_queue("enqueue", content=_task(status="failed", summary="boom")))
        found = scan_transcript(self.path, "session-1")
        self.assertEqual((found[0].status, found[0].summary), ("failed", "boom"))

    def test_mismatched_row_session_rejects_entire_transcript(self):
        self._write(_queue("enqueue", session_id="foreign", content=_task()))
        with self.assertRaises(TranscriptRejected):
            scan_transcript(self.path, "session-1")

    def test_oversized_line_is_rejected(self):
        self.path.write_text("x" * 513, encoding="utf-8")
        with self.assertRaises(TranscriptRejected):
            scan_transcript(self.path, "session-1", max_line_bytes=512)

    def test_malformed_line_rejects_instead_of_skipping_a_possible_consume(self):
        self._write(
            _queue("enqueue", content=_task()),
            "{not-json}\n",
        )
        with self.assertRaises(TranscriptRejected):
            scan_transcript(self.path, "session-1")

    def test_symlink_and_hardlink_transcripts_are_rejected(self):
        real = Path(self.tmp.name) / "real.jsonl"
        real.write_text(_queue("enqueue", content=_task()), encoding="utf-8")
        self.path.symlink_to(real)
        with self.assertRaises(TranscriptRejected):
            scan_transcript(self.path, "session-1")
        self.path.unlink()
        os.link(real, self.path)
        with self.assertRaises(TranscriptRejected):
            scan_transcript(self.path, "session-1")

    def test_route_requires_one_or_two_numeric_components(self):
        self.assertEqual(parse_conversation_route("7"), (7, 7, 7))
        self.assertEqual(parse_conversation_route("7:-100"), ("7:-100", 7, -100))
        for value in ("", "a", "1:2:3", "1:a"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_conversation_route(value)

    def test_marker_is_stable_without_exposing_summary(self):
        self._write(_queue("enqueue", content=_task(summary="private result")))
        item = scan_transcript(self.path, "session-1")[0]
        marker = notification_marker("session-1", item)
        self.assertEqual(marker, notification_marker("session-1", item))
        self.assertNotIn("private", marker)

    def test_telegram_format_is_utf16_bounded(self):
        self._write(_queue("enqueue", content=_task(summary="😀" * 5000)))
        item = scan_transcript(self.path, "session-1")[0]
        text = format_notification(item)
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4000)


class _SessionManager:
    def __init__(self, sessions):
        self.sessions = sessions
        self.update_session = AsyncMock(side_effect=self._update)

    async def list_sessions(self):
        return {key: dict(value) for key, value in self.sessions.items()}

    async def get_session(self, key):
        return dict(self.sessions[key])

    async def _update(self, key, updates):
        self.sessions[key].update(updates)


class DeadSessionScannerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session_id = "session-1"
        (self.root / f"{self.session_id}.jsonl").write_text(
            _queue("enqueue", content=_task()), encoding="utf-8"
        )
        self.sessions = _SessionManager({
            "7:70": {"session_id": self.session_id, "reply_mode": "text"}
        })
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
        self.handler = SimpleNamespace(
            _streams={},
            _get_conversation_lock=lambda _user, _chat: asyncio.Lock(),
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_delivers_once_and_persists_marker(self):
        first = await recover_dead_session_notifications(
            self.bot, self.sessions, self.handler, self.root
        )
        second = await recover_dead_session_notifications(
            self.bot, self.sessions, self.handler, self.root
        )
        self.assertEqual((first.delivered, second.delivered), (1, 0))
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_args.kwargs["chat_id"], 70)
        self.assertEqual(len(self.sessions.sessions["7:70"][MARKER_KEY]), 1)

    async def test_live_stream_and_locked_conversation_are_skipped(self):
        reader = SimpleNamespace(done=lambda: False)
        self.handler._streams[(7, 70)] = SimpleNamespace(reader_task=reader)
        stats = await recover_dead_session_notifications(
            self.bot, self.sessions, self.handler, self.root
        )
        self.assertEqual(stats.skipped_active, 1)
        self.bot.send_message.assert_not_awaited()

        self.handler._streams.clear()
        lock = asyncio.Lock()
        await lock.acquire()
        self.handler._get_conversation_lock = lambda _user, _chat: lock
        stats = await recover_dead_session_notifications(
            self.bot, self.sessions, self.handler, self.root, lock_timeout=0.001
        )
        self.assertEqual(stats.skipped_locked, 1)
        lock.release()

    async def test_invalid_marker_state_fails_closed(self):
        self.sessions.sessions["7:70"][MARKER_KEY] = "corrupt"
        stats = await recover_dead_session_notifications(
            self.bot, self.sessions, self.handler, self.root
        )
        self.assertEqual(stats.rejected, 1)
        self.bot.send_message.assert_not_awaited()

    async def test_send_failure_is_bounded_and_not_marked(self):
        self.bot.send_message.side_effect = RuntimeError("network")
        stats = await recover_dead_session_notifications(
            self.bot, self.sessions, self.handler, self.root
        )
        self.assertEqual(stats.failed, 1)
        self.assertNotIn(MARKER_KEY, self.sessions.sessions["7:70"])

    async def test_global_delivery_attempt_cap_bounds_one_scan(self):
        path = self.root / "session-2.jsonl"
        path.write_text(
            _queue("enqueue", session_id="session-2", content=_task("task-2")),
            encoding="utf-8",
        )
        self.sessions.sessions["8:80"] = {
            "session_id": "session-2",
            "reply_mode": "text",
        }
        stats = await recover_dead_session_notifications(
            self.bot,
            self.sessions,
            self.handler,
            self.root,
            max_delivery_attempts_per_scan=1,
        )
        self.assertEqual(stats.delivered, 1)
        self.assertEqual(self.bot.send_message.await_count, 1)


if __name__ == "__main__":
    unittest.main()
