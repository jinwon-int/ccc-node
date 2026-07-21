"""Opt-in dead-session wakeup (#364 P2): detection rule, gates, and wiring.

Detection fixtures replay real transcript shapes (queue-operation FIFO rows
plus assistant rows) to pin the "unconsumed" rule: a remaining terminal
task-notification whose enqueue timestamp is newer than the last assistant
output. Scan tests drive the bounded gauntlet (disabled flag, quarantine,
cooldown, attempts cap, #388 budget gate) with fakes, and the wiring test runs
a real ``ProjectChatHandler`` over a fake agent runtime to prove the wakeup
turn goes through ``process_message`` with the nudge and delivers via the
#601 unsolicited/notification-bot route.

Module name deliberately sorts before the project_chat test modules that
inject spec-less ``claude_agent_sdk`` stubs, so the module-scope handler
import binds the real SDK generation (same collection-order contract as
``test_project_chat_codex``).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

from telegram_bot.core.agent_runtime import (
    AgentEvent,
    ApprovalHandler,
    CompletionEvent,
    ModelInfo,
    SessionRequest,
    TextDeltaEvent,
    deny_approval,
)
from telegram_bot.core.dead_session_recovery import (
    MARKER_KEY,
    QUARANTINE_KEY,
    recover_dead_session_notifications,
)
from telegram_bot.core.dead_session_wakeup import (
    WAKEUP_NUDGE,
    WAKEUP_STATE_KEY,
    TranscriptRejected,
    find_unconsumed_notifications,
    recovery_should_defer_to_wakeup,
    run_dead_session_wakeup_scan,
)
from telegram_bot.core.project_chat import ProjectChatHandler
from telegram_bot.core.usage_meter import MODE_AUTONOMOUS

SESSION = "session-1"
T0 = "2026-07-19T10:00:00.000Z"
T1 = "2026-07-19T11:00:00.000Z"
T2 = "2026-07-19T12:00:00.000Z"
NOW = datetime(2026, 7, 19, 13, 0, 0, tzinfo=timezone.utc)


def _task(task_id: str = "task-1", status: str = "completed", summary: str = "done") -> str:
    return (
        "<task-notification>"
        f"<task-id>{task_id}</task-id>"
        f"<status>{status}</status>"
        f"<summary>{summary}</summary>"
        "</task-notification>"
    )


def _queue_row(
    operation: str,
    timestamp: Optional[str],
    content: Optional[str] = None,
    session_id: str = SESSION,
) -> str:
    row: dict[str, Any] = {
        "type": "queue-operation",
        "operation": operation,
        "sessionId": session_id,
    }
    if timestamp is not None:
        row["timestamp"] = timestamp
    if content is not None:
        row["content"] = content
    return json.dumps(row) + "\n"


def _assistant_row(timestamp: str) -> str:
    return (
        json.dumps(
            {
                "type": "assistant",
                "timestamp": timestamp,
                "message": {"role": "assistant", "content": []},
            }
        )
        + "\n"
    )


class DetectionRuleTests(unittest.TestCase):
    """The precise "unconsumed notification" rule on transcript fixtures."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / f"{SESSION}.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, *lines: str) -> None:
        self.path.write_text("".join(lines), encoding="utf-8")

    def test_notification_newer_than_last_assistant_output_is_unconsumed(self) -> None:
        self._write(
            _assistant_row(T1),
            _queue_row("enqueue", T2, content=_task()),
        )
        candidate = find_unconsumed_notifications(self.path, SESSION)
        assert candidate is not None
        self.assertEqual(candidate.count, 1)
        self.assertEqual(
            candidate.newest_enqueue_at,
            datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc),
        )

    def test_dequeued_notification_is_consumed(self) -> None:
        self._write(
            _assistant_row(T0),
            _queue_row("enqueue", T1, content=_task()),
            _queue_row("dequeue", T2),
            _assistant_row(T2),
        )
        self.assertIsNone(find_unconsumed_notifications(self.path, SESSION))

    def test_pending_entry_older_than_later_assistant_output_is_ignored(self) -> None:
        # A remaining FIFO entry the CLI had a chance to consume (a later
        # assistant turn exists) is an anomaly; waking for it is not defensible.
        self._write(
            _queue_row("enqueue", T1, content=_task()),
            _assistant_row(T2),
        )
        self.assertIsNone(find_unconsumed_notifications(self.path, SESSION))

    def test_transcript_without_assistant_rows_counts_pending_notification(self) -> None:
        self._write(_queue_row("enqueue", T1, content=_task()))
        candidate = find_unconsumed_notifications(self.path, SESSION)
        assert candidate is not None
        self.assertEqual(candidate.count, 1)

    def test_pending_entry_without_timestamp_is_ignored(self) -> None:
        self._write(
            _assistant_row(T0),
            _queue_row("enqueue", None, content=_task()),
        )
        self.assertIsNone(find_unconsumed_notifications(self.path, SESSION))

    def test_non_terminal_and_plain_pending_content_is_ignored(self) -> None:
        running = (
            "<task-notification><task-id>task-9</task-id>"
            "<status>running</status><summary>still going</summary>"
            "</task-notification>"
        )
        self._write(
            _queue_row("enqueue", T1, content="plain queued user text"),
            _queue_row("enqueue", T2, content=running),
        )
        self.assertIsNone(find_unconsumed_notifications(self.path, SESSION))

    def test_newest_of_multiple_unconsumed_notifications_wins(self) -> None:
        self._write(
            _assistant_row(T0),
            _queue_row("enqueue", T1, content=_task("task-1")),
            _queue_row("enqueue", T2, content=_task("task-2")),
        )
        candidate = find_unconsumed_notifications(self.path, SESSION)
        assert candidate is not None
        self.assertEqual(candidate.count, 2)
        self.assertEqual(
            candidate.newest_enqueue_at,
            datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc),
        )

    def test_foreign_session_rows_reject_the_transcript(self) -> None:
        self._write(_queue_row("enqueue", T1, content=_task(), session_id="foreign"))
        with self.assertRaises(TranscriptRejected):
            find_unconsumed_notifications(self.path, SESSION)


class _FakeSessionManager:
    def __init__(
        self,
        sessions: dict[str, dict[str, Any]],
        journal: list[str] | None = None,
    ) -> None:
        self.sessions = sessions
        self.updates: list[tuple[Any, dict[str, Any]]] = []
        self.journal = journal if journal is not None else []

    async def list_sessions(self) -> dict[str, dict[str, Any]]:
        self.journal.append("list_sessions")
        return {key: dict(value) for key, value in self.sessions.items()}

    async def get_session(self, key: Any) -> dict[str, Any]:
        return dict(self.sessions[str(key)])

    async def update_session(self, key: Any, data: dict[str, Any]) -> None:
        self.journal.append("update_session")
        self.updates.append((key, data))
        self.sessions.setdefault(str(key), {}).update(data)


class _FakeHandler:
    def __init__(
        self,
        response: Any | None = None,
        journal: list[str] | None = None,
    ) -> None:
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._streams: dict[tuple[int, int], Any] = {}
        self._agent_sessions: dict[tuple[int, int], Any] = {}
        self._agent_active_sessions: dict[tuple[int, int], Any] = {}
        self.journal = journal if journal is not None else []
        self._response = response or SimpleNamespace(
            content="wakeup report",
            success=True,
            error=None,
            session_id=SESSION,
            streamed=False,
        )
        self.calls: list[dict[str, Any]] = []

    def _stream_key(self, user_id: int, chat_id: int) -> tuple[int, int]:
        return (user_id, chat_id)

    def _get_conversation_lock(self, user_id: int, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault((user_id, chat_id), asyncio.Lock())

    async def process_message(self, **kwargs: Any) -> Any:
        self.journal.append("process_message")
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _FakeBot:
    def __init__(self) -> None:
        self.send_message = AsyncMock()


class _BlockedMeter:
    def check_autonomous_spend(self, provider: str) -> Any:
        assert provider == "claude"
        return SimpleNamespace(allowed=False, reason=lambda: "claude budget exhausted")


def _session_record(**extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "provider": "claude",
        "session_id": SESSION,
        "model": "opus",
    }
    record.update(extra)
    return record


class WakeupScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conversations_dir = Path(self.tmp.name)
        self.transcript = self.conversations_dir / f"{SESSION}.jsonl"
        self.transcript.write_text(
            _assistant_row(T0) + _queue_row("enqueue", T1, content=_task()),
            encoding="utf-8",
        )
        self.bot = _FakeBot()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _scan(self, handler: Any, manager: Any, **kwargs: Any):
        kwargs.setdefault("enabled", True)
        kwargs.setdefault("now", lambda: NOW)
        return asyncio.run(
            run_dead_session_wakeup_scan(
                self.bot,
                manager,
                handler,
                self.conversations_dir,
                **kwargs,
            )
        )

    def test_disabled_flag_is_a_complete_no_op(self) -> None:
        handler = _FakeHandler()
        manager = _FakeSessionManager({"7": _session_record()})
        stats = self._scan(handler, manager, enabled=False)
        self.assertEqual(manager.journal, [])
        self.assertEqual(handler.calls, [])
        self.assertEqual(stats.triggered, 0)
        self.assertEqual(stats.scanned, 0)

    def test_wakeup_runs_one_autonomous_turn_and_delivers(self) -> None:
        journal: list[str] = []
        handler = _FakeHandler(journal=journal)
        manager = _FakeSessionManager({"7": _session_record()}, journal=journal)
        stats = self._scan(handler, manager)

        self.assertEqual(stats.scanned, 1)
        self.assertEqual(stats.triggered, 1)
        self.assertEqual(stats.delivered, 1)
        self.assertEqual(len(handler.calls), 1)
        call = handler.calls[0]
        self.assertEqual(call["user_message"], WAKEUP_NUDGE)
        self.assertEqual(call["usage_mode"], MODE_AUTONOMOUS)
        self.assertEqual(call["session_id"], SESSION)
        self.assertEqual(call["model"], "opus")
        self.assertFalse(call["new_session"])
        self.assertIs(call["notification_bot"], self.bot)
        self.bot.send_message.assert_awaited_once_with(chat_id=7, text="wakeup report")
        # The attempt record was persisted durably BEFORE the turn started.
        self.assertLess(
            journal.index("update_session"), journal.index("process_message")
        )
        key, payload = manager.updates[0]
        self.assertEqual(key, 7)
        state = payload[WAKEUP_STATE_KEY]
        self.assertEqual(state["session_id"], SESSION)
        self.assertEqual(state["attempts"], 1)

    def test_consumed_notification_triggers_nothing(self) -> None:
        self.transcript.write_text(
            _queue_row("enqueue", T1, content=_task())
            + _queue_row("dequeue", T2)
            + _assistant_row(T2),
            encoding="utf-8",
        )
        handler = _FakeHandler()
        manager = _FakeSessionManager({"7": _session_record()})
        stats = self._scan(handler, manager)
        self.assertEqual(stats.scanned, 1)
        self.assertEqual(stats.triggered, 0)
        self.assertEqual(handler.calls, [])
        self.assertEqual(manager.updates, [])

    def test_quarantined_transcript_is_skipped(self) -> None:
        handler = _FakeHandler()
        manager = _FakeSessionManager(
            {
                "7": _session_record(
                    **{
                        QUARANTINE_KEY: {
                            "session_id": SESSION,
                            "fingerprint": "sha256:abc",
                            "reason": "transcript contains a malformed line",
                        }
                    }
                )
            }
        )
        stats = self._scan(handler, manager)
        self.assertEqual(stats.skipped_quarantine, 1)
        self.assertEqual(stats.triggered, 0)
        self.assertEqual(handler.calls, [])

    def test_cooldown_skips_recent_attempt(self) -> None:
        handler = _FakeHandler()
        recent = NOW.isoformat().replace("+00:00", "Z")
        manager = _FakeSessionManager(
            {
                "7": _session_record(
                    **{
                        WAKEUP_STATE_KEY: {
                            "session_id": SESSION,
                            "attempts": 1,
                            "last_attempt_at": recent,
                        }
                    }
                )
            }
        )
        stats = self._scan(handler, manager, cooldown_seconds=600.0)
        self.assertEqual(stats.skipped_cooldown, 1)
        self.assertEqual(handler.calls, [])

    def test_attempts_cap_skips_exhausted_session(self) -> None:
        handler = _FakeHandler()
        manager = _FakeSessionManager(
            {
                "7": _session_record(
                    **{
                        WAKEUP_STATE_KEY: {
                            "session_id": SESSION,
                            "attempts": 2,
                            "last_attempt_at": T0,
                        }
                    }
                )
            }
        )
        stats = self._scan(handler, manager, max_attempts_per_session=2)
        self.assertEqual(stats.skipped_attempts, 1)
        self.assertEqual(handler.calls, [])

    def test_attempts_reset_when_session_rotates(self) -> None:
        handler = _FakeHandler()
        manager = _FakeSessionManager(
            {
                "7": _session_record(
                    **{
                        WAKEUP_STATE_KEY: {
                            "session_id": "previous-session",
                            "attempts": 2,
                            "last_attempt_at": T0,
                        }
                    }
                )
            }
        )
        stats = self._scan(handler, manager, max_attempts_per_session=2)
        self.assertEqual(stats.triggered, 1)
        self.assertEqual(manager.updates[0][1][WAKEUP_STATE_KEY]["attempts"], 1)

    def test_budget_gate_blocks_autonomous_wakeup(self) -> None:
        handler = _FakeHandler()
        manager = _FakeSessionManager({"7": _session_record()})
        stats = self._scan(handler, manager, usage_meter=_BlockedMeter())
        self.assertEqual(stats.skipped_budget, 1)
        self.assertEqual(stats.triggered, 0)
        self.assertEqual(handler.calls, [])
        self.assertEqual(manager.updates, [])

    def test_codex_session_is_out_of_scope(self) -> None:
        handler = _FakeHandler()
        manager = _FakeSessionManager({"7": _session_record(provider="codex")})
        stats = self._scan(handler, manager)
        self.assertEqual(stats.scanned, 0)
        self.assertEqual(handler.calls, [])

    def test_live_agent_session_is_not_woken(self) -> None:
        handler = _FakeHandler()
        handler._agent_sessions[(7, 7)] = object()
        manager = _FakeSessionManager({"7": _session_record()})
        stats = self._scan(handler, manager)
        self.assertEqual(stats.skipped_active, 1)
        self.assertEqual(handler.calls, [])

    def test_dead_wakeup_turn_still_consumes_attempt_budget(self) -> None:
        handler = _FakeHandler(response=RuntimeError("bridge died mid-turn"))
        manager = _FakeSessionManager({"7": _session_record()})
        stats = self._scan(handler, manager)
        self.assertEqual(stats.failed, 1)
        self.assertEqual(stats.triggered, 0)
        # The persisted attempt keeps a crashing wakeup from looping.
        self.assertEqual(manager.updates[0][1][WAKEUP_STATE_KEY]["attempts"], 1)
        self.bot.send_message.assert_not_awaited()

    def test_max_wakeups_per_scan_bounds_the_tick(self) -> None:
        second = self.conversations_dir / "session-2.jsonl"
        second.write_text(
            json.dumps(
                {
                    "type": "queue-operation",
                    "operation": "enqueue",
                    "sessionId": "session-2",
                    "timestamp": T1,
                    "content": _task(),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        handler = _FakeHandler()
        manager = _FakeSessionManager(
            {
                "7": _session_record(),
                "8": _session_record(session_id="session-2"),
            }
        )
        stats = self._scan(handler, manager, max_wakeups_per_scan=1)
        self.assertEqual(stats.triggered, 1)
        self.assertEqual(len(handler.calls), 1)


class RecoveryWakeupDedupTests(unittest.TestCase):
    """#620 wakeup-first hand-off: recovery defers, wakeup claims, fallback.

    One stranded notification must produce exactly one user-facing message:
    the wakeup's autonomous report when the wakeup can claim it, or the P1
    raw replay whenever the wakeup is off, skipped, exhausted, or fails —
    never both, and never neither.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conversations_dir = Path(self.tmp.name)
        self.transcript = self.conversations_dir / f"{SESSION}.jsonl"
        self.transcript.write_text(
            _assistant_row(T0) + _queue_row("enqueue", T1, content=_task()),
            encoding="utf-8",
        )
        self.bot = _FakeBot()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _defer(
        self,
        handler: Any,
        usage_meter: Any = None,
        max_attempts_per_session: int = 2,
    ):
        """Mirror bot_lifecycle._build_dead_session_wakeup_defer's closure."""

        def wakeup_defer(current, session_id, replay, user_id, chat_id) -> bool:
            return recovery_should_defer_to_wakeup(
                handler,
                current,
                session_id,
                replay,
                user_id,
                chat_id,
                usage_meter=usage_meter,
                max_attempts_per_session=max_attempts_per_session,
            )

        return wakeup_defer

    async def _recover(self, handler: Any, manager: Any, wakeup_defer: Any = None):
        return await recover_dead_session_notifications(
            self.bot, manager, handler, self.conversations_dir, wakeup_defer=wakeup_defer
        )

    def _raw_replay_text(self) -> str:
        return self.bot.send_message.await_args.kwargs["text"]

    def test_flag_on_eligible_recovery_defers_and_wakeup_claims(self) -> None:
        handler = _FakeHandler()
        manager = _FakeSessionManager({"7": _session_record()})
        defer = self._defer(handler)

        async def scenario() -> None:
            # Tick N: recovery defers the raw replay instead of delivering.
            first = await self._recover(handler, manager, defer)
            self.assertEqual(first.scanned, 1)
            self.assertEqual(first.deferred_wakeup, 1)
            self.assertEqual(first.delivered, 0)
            self.bot.send_message.assert_not_awaited()
            # Deferral consumes nothing: no delivery marker was written.
            self.assertNotIn(MARKER_KEY, manager.sessions["7"])

            # Same tick, after the recovery scan: the wakeup claims it.
            wakeup = await run_dead_session_wakeup_scan(
                self.bot,
                manager,
                handler,
                self.conversations_dir,
                enabled=True,
                now=lambda: NOW,
            )
            self.assertEqual(wakeup.triggered, 1)
            self.assertEqual(wakeup.delivered, 1)

            # The resumed CLI turn dequeues the injected notification and
            # appends its assistant reply (contract pinned by
            # DetectionRuleTests.test_dequeued_notification_is_consumed and
            # TranscriptRecoveryParserTests); simulate those transcript rows.
            with self.transcript.open("a", encoding="utf-8") as stream:
                stream.write(_queue_row("dequeue", T2))
                stream.write(_assistant_row(T2))

            # Tick N+1: recovery finds nothing left — no raw replay, ever.
            second = await self._recover(handler, manager, defer)
            self.assertEqual(second.scanned, 1)
            self.assertEqual((second.delivered, second.deferred_wakeup), (0, 0))
            self.bot.send_message.assert_awaited_once_with(
                chat_id=7, text="wakeup report"
            )

        asyncio.run(scenario())

    def test_flag_off_default_keeps_raw_replay_unchanged(self) -> None:
        handler = _FakeHandler()
        manager = _FakeSessionManager({"7": _session_record()})

        async def scenario() -> None:
            stats = await self._recover(handler, manager, wakeup_defer=None)
            self.assertEqual((stats.delivered, stats.deferred_wakeup), (1, 0))
            self.assertEqual(handler.calls, [])
            self.bot.send_message.assert_awaited_once()
            self.assertTrue(
                self._raw_replay_text().startswith("✅ Background task completed")
            )

        asyncio.run(scenario())

    def test_budget_blocked_wakeup_falls_back_to_raw_delivery(self) -> None:
        handler = _FakeHandler()
        manager = _FakeSessionManager({"7": _session_record()})
        meter = _BlockedMeter()
        defer = self._defer(handler, usage_meter=meter)

        async def scenario() -> None:
            stats = await self._recover(handler, manager, defer)
            self.assertEqual((stats.delivered, stats.deferred_wakeup), (1, 0))
            self.assertTrue(
                self._raw_replay_text().startswith("✅ Background task completed")
            )
            # The wakeup scan refuses the same conversation: exactly one
            # user-facing message in total.
            wakeup = await run_dead_session_wakeup_scan(
                self.bot,
                manager,
                handler,
                self.conversations_dir,
                enabled=True,
                usage_meter=meter,
                now=lambda: NOW,
            )
            self.assertEqual(wakeup.skipped_budget, 1)
            self.assertEqual(handler.calls, [])
            self.assertEqual(self.bot.send_message.await_count, 1)

        asyncio.run(scenario())

    def test_exhausted_wakeup_attempts_fall_back_to_raw_delivery(self) -> None:
        handler = _FakeHandler()
        manager = _FakeSessionManager(
            {
                "7": _session_record(
                    **{
                        WAKEUP_STATE_KEY: {
                            "session_id": SESSION,
                            "attempts": 2,
                            "last_attempt_at": T0,
                        }
                    }
                )
            }
        )
        defer = self._defer(handler, max_attempts_per_session=2)

        async def scenario() -> None:
            stats = await self._recover(handler, manager, defer)
            self.assertEqual((stats.delivered, stats.deferred_wakeup), (1, 0))
            self.assertTrue(
                self._raw_replay_text().startswith("✅ Background task completed")
            )

        asyncio.run(scenario())

    def test_failed_wakeup_turn_falls_back_to_raw_delivery_on_a_later_tick(self) -> None:
        handler = _FakeHandler(response=RuntimeError("wakeup turn died"))
        manager = _FakeSessionManager({"7": _session_record()})
        defer = self._defer(handler, max_attempts_per_session=1)

        async def scenario() -> None:
            # Tick N: deferred, then the wakeup claims (attempt persisted
            # durably before the turn) and the turn fails.
            first = await self._recover(handler, manager, defer)
            self.assertEqual((first.deferred_wakeup, first.delivered), (1, 0))
            wakeup = await run_dead_session_wakeup_scan(
                self.bot,
                manager,
                handler,
                self.conversations_dir,
                enabled=True,
                max_attempts_per_session=1,
                now=lambda: NOW,
            )
            self.assertEqual(wakeup.failed, 1)
            self.assertEqual(wakeup.delivered, 0)

            # Tick N+1: attempts exhausted -> recovery stops deferring and
            # delivers the raw replay. Exactly one user-facing message.
            second = await self._recover(handler, manager, defer)
            self.assertEqual((second.deferred_wakeup, second.delivered), (0, 1))
            self.bot.send_message.assert_awaited_once()
            self.assertTrue(
                self._raw_replay_text().startswith("✅ Background task completed")
            )

        asyncio.run(scenario())

    def test_cooldown_alone_keeps_deferring_while_attempts_remain(self) -> None:
        # A cooldown only delays the wakeup's next claim; attempts remain, so
        # recovery keeps deferring instead of racing in a raw replay.
        handler = _FakeHandler()
        recent = NOW.isoformat().replace("+00:00", "Z")
        manager = _FakeSessionManager(
            {
                "7": _session_record(
                    **{
                        WAKEUP_STATE_KEY: {
                            "session_id": SESSION,
                            "attempts": 1,
                            "last_attempt_at": recent,
                        }
                    }
                )
            }
        )
        defer = self._defer(handler, max_attempts_per_session=2)

        async def scenario() -> None:
            stats = await self._recover(handler, manager, defer)
            self.assertEqual((stats.deferred_wakeup, stats.delivered), (1, 0))
            self.bot.send_message.assert_not_awaited()

        asyncio.run(scenario())

    def test_resume_disabled_falls_back_to_raw_delivery(self) -> None:
        handler = _FakeHandler()
        manager = _FakeSessionManager({"7": _session_record()})
        defer = self._defer(handler)

        async def scenario() -> None:
            with patch.dict(os.environ, {"CCC_RESUME_PERSISTED_SESSIONS": "false"}):
                stats = await self._recover(handler, manager, defer)
            self.assertEqual((stats.delivered, stats.deferred_wakeup), (1, 0))

        asyncio.run(scenario())

    def test_live_agent_session_falls_back_to_raw_delivery(self) -> None:
        # The wakeup never wakes a live adapter conversation, so recovery must
        # not defer to it either — pre-#620 behavior is preserved.
        handler = _FakeHandler()
        handler._agent_sessions[(7, 7)] = object()
        manager = _FakeSessionManager({"7": _session_record()})
        defer = self._defer(handler)

        async def scenario() -> None:
            stats = await self._recover(handler, manager, defer)
            self.assertEqual((stats.delivered, stats.deferred_wakeup), (1, 0))

        asyncio.run(scenario())

    def test_deferral_predicate_error_fails_safe_to_raw_delivery(self) -> None:
        handler = _FakeHandler()
        manager = _FakeSessionManager({"7": _session_record()})

        def broken(current, session_id, replay, user_id, chat_id) -> bool:
            raise RuntimeError("predicate exploded")

        async def scenario() -> None:
            stats = await self._recover(handler, manager, broken)
            self.assertEqual((stats.delivered, stats.deferred_wakeup), (1, 0))
            self.bot.send_message.assert_awaited_once()

        asyncio.run(scenario())


class _MeterRecorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, int]]] = []

    def record(self, provider: str, mode: str, **counts: int):
        self.records.append((provider, mode, counts))
        return ()


class AutonomousMeteringTests(unittest.TestCase):
    def test_adapter_attempt_meters_autonomous_mode(self) -> None:
        handler = ProjectChatHandler.__new__(ProjectChatHandler)
        meter = _MeterRecorder()
        handler._usage_meter = meter  # type: ignore[assignment]
        handler._config = SimpleNamespace(agent_provider="claude")
        handler._agent_runtime = SimpleNamespace()
        handler.record_claude_adapter_attempt(mode=MODE_AUTONOMOUS)
        self.assertEqual(meter.records, [("claude", "autonomous", {"requests": 1})])

    def test_adapter_attempt_defaults_to_interactive(self) -> None:
        handler = ProjectChatHandler.__new__(ProjectChatHandler)
        meter = _MeterRecorder()
        handler._usage_meter = meter  # type: ignore[assignment]
        handler._config = SimpleNamespace(agent_provider="claude")
        handler._agent_runtime = SimpleNamespace()
        handler.record_claude_adapter_attempt()
        self.assertEqual(meter.records, [("claude", "interactive", {"requests": 1})])


class _WiringFakeSession:
    """Fake runtime session with the optional unsolicited seam (#601)."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.messages: list[str] = []
        self.unsolicited_handler: Any = None

    def set_unsolicited_handler(self, handler: Any) -> None:
        self.unsolicited_handler = handler

    def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        async def stream() -> AsyncIterator[AgentEvent]:
            self.messages.append(message)
            yield TextDeltaEvent("background task done: PR is green")
            yield CompletionEvent("end_turn")

        return stream()

    async def interrupt(self) -> None:  # pragma: no cover - not exercised
        pass


class _WiringFakeRuntime:
    def __init__(self, session: _WiringFakeSession) -> None:
        self.session = session
        self.requests: list[SessionRequest] = []

    async def start_or_resume(self, request: SessionRequest) -> _WiringFakeSession:
        self.requests.append(request)
        return self.session

    async def list_models(self):  # pragma: no cover - not exercised
        return (ModelInfo("claude-test", "Claude Test"),)

    async def close(self) -> None:  # pragma: no cover - not exercised
        pass


class WakeupWiringTests(unittest.TestCase):
    """End-to-end: scan -> process_message(nudge) -> unsolicited route."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.conversations_dir = root / "conversations"
        self.conversations_dir.mkdir()
        (self.conversations_dir / f"{SESSION}.jsonl").write_text(
            _assistant_row(T0) + _queue_row("enqueue", T1, content=_task()),
            encoding="utf-8",
        )
        settings = SimpleNamespace(
            agent_provider="claude",
            project_root=root,
            execution_profile="strict-project",
            bash_policy="disabled",
            allowed_user_ids=[7],
            require_allowlist=True,
            claude_cli_path=None,
            claude_settings_path=root / "claude" / "settings.json",
            enable_streaming=False,
            enable_partial_streaming=False,
            bot_data_dir=None,
            task_ledger_path=None,
            usage_meter_enabled=False,
        )
        self.session = _WiringFakeSession(SESSION)
        self.runtime = _WiringFakeRuntime(self.session)
        self.handler = ProjectChatHandler(settings=settings, agent_runtime=self.runtime)
        self.handler._task_ledger_cache = False
        self.bot = _FakeBot()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_triggered_wakeup_resumes_and_delivers_via_notification_route(self) -> None:
        manager = _FakeSessionManager({"7": _session_record()})

        async def scenario() -> None:
            stats = await run_dead_session_wakeup_scan(
                self.bot,
                manager,
                self.handler,
                self.conversations_dir,
                enabled=True,
                now=lambda: NOW,
            )
            self.assertEqual(stats.triggered, 1)
            self.assertEqual(stats.delivered, 1)
            # The turn resumed the persisted session with the nudge.
            self.assertEqual(self.session.messages, [WAKEUP_NUDGE])
            self.assertEqual(self.runtime.requests[0].session_id, SESSION)
            # Final turn content was delivered to the conversation.
            self.bot.send_message.assert_awaited_once_with(
                chat_id=7, text="background task done: PR is green"
            )
            # The #601 unsolicited route was registered against the wakeup's
            # notification bot: a between-turns continuation the CLI makes
            # after the nudge turn delivers to the same conversation.
            assert self.session.unsolicited_handler is not None
            await self.session.unsolicited_handler("late autonomous report", SESSION)
            self.assertEqual(self.bot.send_message.await_count, 2)
            self.assertEqual(
                self.bot.send_message.await_args.kwargs["text"],
                "late autonomous report",
            )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
