import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram_bot.core.dead_session_recovery import (
    MARKER_KEY,
    QUARANTINE_KEY,
    TranscriptRejected,
    format_notification,
    notification_marker,
    parse_conversation_route,
    quarantine_blocks_scan,
    recover_dead_session_notifications,
    scan_transcript,
    transcript_quarantine_record,
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

    async def test_codex_session_with_missing_claude_root_is_a_noop(self):
        self.sessions.sessions["7:70"]["provider"] = "codex"
        original = dict(self.sessions.sessions["7:70"])
        missing_root = self.root / "missing-claude-root"

        for _ in range(5):
            stats = await recover_dead_session_notifications(
                self.bot, self.sessions, self.handler, missing_root
            )
            self.assertEqual(
                (
                    stats.scanned,
                    stats.delivered,
                    stats.failed,
                    stats.rejected,
                    stats.quarantined,
                    stats.hard_quarantined,
                ),
                (0, 0, 0, 0, 0, 0),
            )

        self.assertEqual(self.sessions.sessions["7:70"], original)
        self.sessions.update_session.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_mixed_sessions_recover_legacy_claude_and_skip_codex(self):
        self.sessions.sessions["8:80"] = {
            "provider": "codex",
            "session_id": "codex-thread",
            "reply_mode": "text",
        }

        stats = await recover_dead_session_notifications(
            self.bot, self.sessions, self.handler, self.root
        )

        self.assertEqual((stats.scanned, stats.delivered, stats.rejected), (1, 1, 0))
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_args.kwargs["chat_id"], 70)
        self.sessions.update_session.assert_awaited_once()
        self.assertEqual(self.sessions.update_session.await_args.args[0], "7:70")

    async def test_provider_switch_during_scan_does_not_cross_runtime_boundary(self):
        stale_snapshot = await self.sessions.list_sessions()
        self.sessions.sessions["7:70"]["provider"] = "codex"
        self.sessions.list_sessions = AsyncMock(return_value=stale_snapshot)

        stats = await recover_dead_session_notifications(
            self.bot, self.sessions, self.handler, self.root
        )

        self.assertEqual((stats.scanned, stats.delivered, stats.rejected), (0, 0, 0))
        self.sessions.update_session.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()


class TranscriptQuarantineTests(unittest.IsolatedAsyncioTestCase):
    """Rejected transcripts are quarantined instead of rescanned forever (#411 B)."""

    # Redaction canary: stands in for arbitrary transcript content (which may
    # contain prompts or credentials in production) and must never leak into
    # quarantine notices or records.
    CANARY = "REDACTION_CANARY_MARKER_XYZ"

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session_id = "session-1"
        self.path = self.root / f"{self.session_id}.jsonl"
        # Rejects with "queue row session id does not match owner"; the canary
        # inside the row must never leak into notices or records.
        self.path.write_text(
            _queue("enqueue", session_id="foreign", content=self.CANARY),
            encoding="utf-8",
        )
        self.sessions = _SessionManager(
            {"7:70": {"session_id": self.session_id, "reply_mode": "text"}}
        )
        self.bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=1))
        )
        self.handler = SimpleNamespace(
            _streams={},
            _get_conversation_lock=lambda _user, _chat: asyncio.Lock(),
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def _recover(self, **kwargs):
        return await recover_dead_session_notifications(
            self.bot, self.sessions, self.handler, self.root, **kwargs
        )

    def _counting_scan(self):
        import telegram_bot.core.dead_session_recovery as module

        calls = []
        original = scan_transcript

        def wrapper(path, expected_session_id, **kwargs):
            calls.append(str(path))
            return original(path, expected_session_id, **kwargs)

        patcher = unittest.mock.patch.object(module, "scan_transcript", wrapper)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    async def test_rejected_transcript_is_parsed_once_and_notified_once(self):
        self.sessions.sessions["7:70"]["provider"] = "claude"
        calls = self._counting_scan()

        first = await self._recover()
        second = await self._recover()
        third = await self._recover()

        # Exactly one parse across all ticks; only the first tick rejects.
        self.assertEqual(len(calls), 1)
        self.assertEqual((first.rejected, first.quarantined), (1, 1))
        self.assertEqual((second.rejected, second.quarantine_skipped), (0, 1))
        self.assertEqual((third.rejected, third.quarantine_skipped), (0, 1))
        # Exactly one owner notice, deduplicated afterwards.
        self.bot.send_message.assert_awaited_once()
        text = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("quarantined", text)
        self.assertIn("queue row session id does not match owner", text)
        record = self.sessions.sessions["7:70"][QUARANTINE_KEY]
        self.assertTrue(record["notified"])
        self.assertTrue(record["fingerprint"].startswith("sha256:"))

    async def test_notice_and_record_never_leak_transcript_content(self):
        await self._recover()

        text = self.bot.send_message.await_args.kwargs["text"]
        record = json.dumps(self.sessions.sessions["7:70"][QUARANTINE_KEY])
        self.assertNotIn(self.CANARY, text)
        self.assertNotIn(self.CANARY, record)
        self.assertNotIn(str(self.root), text)

    async def test_quarantine_survives_restart(self):
        await self._recover()
        calls = self._counting_scan()

        # A fresh manager over the same persisted store simulates a restart.
        restarted = _SessionManager(self.sessions.sessions)
        stats = await recover_dead_session_notifications(
            self.bot, restarted, self.handler, self.root
        )

        self.assertEqual(stats.quarantine_skipped, 1)
        self.assertEqual(calls, [])
        self.bot.send_message.assert_awaited_once()

    async def test_changed_transcript_is_reevaluated_and_recovers(self):
        await self._recover()
        # Repair the transcript: recovery must notice the identity change,
        # re-scan, deliver, and lift the quarantine.
        self.path.write_text(_queue("enqueue", content=_task()), encoding="utf-8")

        stats = await self._recover()

        self.assertEqual((stats.scanned, stats.delivered), (1, 1))
        self.assertIsNone(self.sessions.sessions["7:70"][QUARANTINE_KEY])

    async def test_changed_but_still_rejecting_transcript_requarantines_once(self):
        await self._recover()
        old_fingerprint = self.sessions.sessions["7:70"][QUARANTINE_KEY]["fingerprint"]
        self.path.write_text(
            _queue("enqueue", session_id="foreign", content="different body"),
            encoding="utf-8",
        )

        second = await self._recover()
        third = await self._recover()

        self.assertEqual((second.rejected, second.quarantined), (1, 1))
        self.assertEqual((third.rejected, third.quarantine_skipped), (0, 1))
        record = self.sessions.sessions["7:70"][QUARANTINE_KEY]
        self.assertNotEqual(record["fingerprint"], old_fingerprint)
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_failed_notice_retries_without_reparsing(self):
        calls = self._counting_scan()
        self.bot.send_message.side_effect = RuntimeError("network down")

        first = await self._recover()
        self.bot.send_message.side_effect = None
        second = await self._recover()
        third = await self._recover()

        self.assertEqual(len(calls), 1)
        self.assertEqual((first.quarantined, first.failed), (1, 1))
        self.assertEqual(second.quarantine_skipped, 1)
        self.assertTrue(self.sessions.sessions["7:70"][QUARANTINE_KEY]["notified"])
        self.assertEqual(third.quarantine_skipped, 1)
        # First send failed, second succeeded, third deduplicated.
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_identityless_rejection_still_retries_pending_notice(self):
        """#424 review: records with identity=null never take the pre-scan
        skip, so the failed-notice retry must stay reachable on the
        same-fingerprint dedupe path too.

        ``root_retry_attempts=1`` disables the startup-race retry (#541):
        this test is about the notice-retry/dedupe behavior once the root is
        settled as permanently missing, not about the retry timing itself.
        """
        missing_root = self.root / "missing"  # -> "conversation root unavailable"
        self.bot.send_message.side_effect = RuntimeError("network down")

        first = await recover_dead_session_notifications(
            self.bot, self.sessions, self.handler, missing_root, root_retry_attempts=1
        )
        self.bot.send_message.side_effect = None
        second = await recover_dead_session_notifications(
            self.bot, self.sessions, self.handler, missing_root, root_retry_attempts=1
        )
        third = await recover_dead_session_notifications(
            self.bot, self.sessions, self.handler, missing_root, root_retry_attempts=1
        )

        record = self.sessions.sessions["7:70"][QUARANTINE_KEY]
        self.assertIsNone(record["identity"])
        self.assertTrue(record["notified"])
        self.assertEqual((first.quarantined, first.failed), (1, 1))
        # One failed attempt, one successful retry, then deduplicated.
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual((second.quarantined, third.quarantined), (0, 0))
        text = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("conversation root unavailable", text)

    async def test_transient_root_race_recovers_without_quarantine(self):
        """The Termux/Android startup storage race (#541): a conversations
        root that is not yet stat-able when the scan starts, but appears
        within the retry budget, must recover normally with no quarantine
        and no owner notice at all."""
        settling_root = self.root / "settling-root"

        async def _create_after_delay():
            await asyncio.sleep(0.01)
            settling_root.mkdir()
            (settling_root / f"{self.session_id}.jsonl").write_text(
                _queue("enqueue", content=_task()), encoding="utf-8"
            )

        creator = asyncio.ensure_future(_create_after_delay())
        try:
            stats = await recover_dead_session_notifications(
                self.bot,
                self.sessions,
                self.handler,
                settling_root,
                root_retry_attempts=5,
                root_retry_backoff_seconds=0.01,
            )
        finally:
            await creator

        self.assertEqual(
            (stats.scanned, stats.delivered, stats.rejected, stats.quarantined),
            (1, 1, 0, 0),
        )
        self.assertNotIn(QUARANTINE_KEY, self.sessions.sessions["7:70"])
        self.bot.send_message.assert_awaited_once()
        text = self.bot.send_message.await_args.kwargs["text"]
        self.assertNotIn("quarantined", text)

    async def test_permanently_missing_root_still_quarantines_after_retry_budget(self):
        """A root that never appears must still fail safe (quarantine) once
        the bounded retry budget is exhausted -- the retry only absorbs a
        transient race, it never masks a real removal/corruption."""
        missing_root = self.root / "permanently-missing"

        stats = await recover_dead_session_notifications(
            self.bot,
            self.sessions,
            self.handler,
            missing_root,
            root_retry_attempts=2,
            root_retry_backoff_seconds=0.01,
        )

        self.assertEqual((stats.rejected, stats.quarantined), (1, 1))
        record = self.sessions.sessions["7:70"][QUARANTINE_KEY]
        self.assertEqual(record["reason"], "conversation root unavailable")

    def _churn_identity(self, tick):
        # Rewrite the transcript with a different byte length so the file's
        # lstat identity (size, mtime) changes every tick — the identity gate
        # would NOT block, isolating the hard-quarantine bound (#423).
        self.path.write_text(
            _queue("enqueue", session_id="foreign", content=self.CANARY + "x" * tick),
            encoding="utf-8",
        )

    async def test_consecutive_rejects_hard_quarantine_stops_reparsing(self):
        """#423 (a): after N content-deterministic rejects with churning file
        identity, hard-quarantine and stop re-parsing entirely."""
        calls = self._counting_scan()
        max_rejects = 3

        with self.assertLogs(
            "telegram_bot.core.dead_session_recovery", level="WARNING"
        ) as logs:
            for tick in range(max_rejects):
                self._churn_identity(tick)
                stats = await self._recover(max_rejects=max_rejects)
                record = self.sessions.sessions["7:70"][QUARANTINE_KEY]
                if tick < max_rejects - 1:
                    # Soft quarantine: parsed (identity drifted, gate open),
                    # counted as a quarantine EVENT each tick, but not yet hard.
                    self.assertEqual(stats.rejected, 1)
                    self.assertEqual(stats.quarantined, 1)
                    self.assertEqual(stats.hard_quarantined, 0)
                    self.assertFalse(record["hard"])
                    self.assertEqual(record["reject_count"], tick + 1)
                else:
                    # Tick N: crosses the bound -> hard, give-up counted once.
                    self.assertEqual(stats.rejected, 1)
                    self.assertEqual(stats.quarantined, 1)
                    self.assertEqual(stats.hard_quarantined, 1)
                    self.assertTrue(record["hard"])
                    self.assertGreaterEqual(record["reject_count"], max_rejects)

            # Parsing happened up to N (one per soft/hard-transition tick).
            self.assertEqual(len(calls), max_rejects)
            hard_await_count = self.bot.send_message.await_count

            # Subsequent ticks: hard block regardless of identity drift. No
            # re-parse, no re-notify, counted only as quarantine_skipped.
            for tick in range(max_rejects, max_rejects + 3):
                self._churn_identity(tick)
                stats = await self._recover(max_rejects=max_rejects)
                self.assertEqual(stats.quarantine_skipped, 1)
                self.assertEqual(stats.rejected, 0)
                self.assertEqual(stats.quarantined, 0)
                self.assertEqual(stats.hard_quarantined, 0)
                self.assertEqual(len(calls), max_rejects)  # no re-parse

        # No further owner notice after the conversation went hard.
        self.assertEqual(self.bot.send_message.await_count, hard_await_count)
        # Exactly one hard-quarantine summary WARNING line.
        summary_lines = [
            line for line in logs.output if "hard-quarantined after" in line
        ]
        self.assertEqual(len(summary_lines), 1)
        # Reason code only — never transcript content.
        self.assertIn("queue row session id does not match owner", summary_lines[0])
        self.assertNotIn(self.CANARY, summary_lines[0])

    async def test_transient_failure_does_not_advance_reject_counter(self):
        """#423 (b): a non-TranscriptRejected failure (here get_session raising)
        must never climb the counter toward a hard quarantine."""

        class _FlakyManager(_SessionManager):
            async def get_session(self, key):
                raise RuntimeError("momentary FS error")

        flaky = _FlakyManager(self.sessions.sessions)
        for _ in range(5):
            stats = await recover_dead_session_notifications(
                self.bot, flaky, self.handler, self.root, max_rejects=1
            )
            # Folds into rejected (transient), never quarantines.
            self.assertEqual(stats.rejected, 1)
            self.assertEqual(stats.quarantined, 0)
            self.assertEqual(stats.hard_quarantined, 0)

        # No quarantine record was ever written; nothing hard-quarantined.
        self.assertNotIn(QUARANTINE_KEY, self.sessions.sessions["7:70"])
        self.bot.send_message.assert_not_awaited()

    async def test_send_failure_does_not_advance_reject_counter(self):
        """#423 (b, variant): a scan-delivery send failure (stats.failed) on a
        valid transcript must not advance the reject counter."""
        # Valid, parseable transcript with one terminal notification.
        self.path.write_text(_queue("enqueue", content=_task()), encoding="utf-8")
        self.bot.send_message.side_effect = RuntimeError("network")

        for _ in range(5):
            stats = await recover_dead_session_notifications(
                self.bot, self.sessions, self.handler, self.root, max_rejects=1
            )
            self.assertEqual(stats.failed, 1)
            self.assertEqual(stats.rejected, 0)
            self.assertEqual(stats.quarantined, 0)
            self.assertEqual(stats.hard_quarantined, 0)

        # A send failure is transient: no quarantine record is written.
        self.assertNotIn(QUARANTINE_KEY, self.sessions.sessions["7:70"])

    async def test_hard_quarantine_persists_across_restart(self):
        """#423 (c): a hard quarantine survives a restart and keeps blocking."""
        max_rejects = 2
        for tick in range(max_rejects):
            self._churn_identity(tick)
            await self._recover(max_rejects=max_rejects)
        record = self.sessions.sessions["7:70"][QUARANTINE_KEY]
        self.assertTrue(record["hard"])
        self.assertGreaterEqual(record["reject_count"], max_rejects)

        calls = self._counting_scan()
        # Fresh manager over the same persisted store simulates a restart.
        restarted = _SessionManager(self.sessions.sessions)
        # Churn identity again to prove the block is identity-independent.
        self._churn_identity(max_rejects + 1)
        stats = await recover_dead_session_notifications(
            self.bot, restarted, self.handler, self.root, max_rejects=max_rejects
        )

        self.assertEqual(stats.quarantine_skipped, 1)
        self.assertEqual(calls, [])  # no re-parse
        persisted = self.sessions.sessions["7:70"][QUARANTINE_KEY]
        self.assertTrue(persisted["hard"])
        self.assertGreaterEqual(persisted["reject_count"], max_rejects)

    def test_quarantine_blocks_scan_requires_matching_identity(self):
        record = transcript_quarantine_record(self.session_id, "reason", self.path)
        self.assertTrue(quarantine_blocks_scan(record, self.session_id, self.path))
        self.assertFalse(quarantine_blocks_scan(record, "other-session", self.path))
        self.assertFalse(quarantine_blocks_scan(None, self.session_id, self.path))
        self.assertFalse(quarantine_blocks_scan("corrupt", self.session_id, self.path))
        # Advance mtime by a whole second explicitly: a bare os.utime() sets it
        # to "now", which on coarse-resolution filesystems can equal the prior
        # mtime and leave the identity unchanged (flaky on Termux/fast FS).
        future = time.time() + 1
        os.utime(self.path, (future, future))  # identity drift re-enables evaluation
        self.assertFalse(quarantine_blocks_scan(record, self.session_id, self.path))

    def test_fingerprint_is_stable_and_identity_bound(self):
        first = transcript_quarantine_record(self.session_id, "reason", self.path)
        second = transcript_quarantine_record(self.session_id, "reason", self.path)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        other_reason = transcript_quarantine_record(self.session_id, "other", self.path)
        self.assertNotEqual(first["fingerprint"], other_reason["fingerprint"])
        pathless = transcript_quarantine_record(self.session_id, "reason", None)
        self.assertNotEqual(first["fingerprint"], pathless["fingerprint"])
        self.assertIsNone(pathless["identity"])


if __name__ == "__main__":
    unittest.main()
