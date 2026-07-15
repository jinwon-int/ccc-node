"""Deterministic tests for the provider-neutral Codex runtime adapter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
from typing import TYPE_CHECKING, Any, cast
import unittest

if TYPE_CHECKING:
    from core.agent_runtime import (
        AgentEvent,
        AgentRuntime,
        ApprovalDecision,
        ApprovalRequestEvent,
        CompletionEvent,
        MessageCompletedEvent,
        ModelInfo,
        ReasoningDeltaEvent,
        ResultEvent,
        SessionRequest,
        TextDeltaEvent,
        ToolCompletedEvent,
        ToolStartedEvent,
    )
    from core.codex_app_server import CodexNotification, CodexServerRequest
    from core.codex_runtime import CodexRuntime, _run_codex_memory_bootstrap
    from core.usage import SNAPSHOT_TTL_SECONDS, UsageSnapshot
else:
    from telegram_bot.core.agent_runtime import (
        AgentEvent,
        AgentRuntime,
        ApprovalDecision,
        ApprovalRequestEvent,
        CompletionEvent,
        MessageCompletedEvent,
        ModelInfo,
        ReasoningDeltaEvent,
        ResultEvent,
        SessionRequest,
        TextDeltaEvent,
        ToolCompletedEvent,
        ToolStartedEvent,
    )
    from telegram_bot.core.codex_app_server import CodexNotification, CodexServerRequest
    from telegram_bot.core.codex_runtime import CodexRuntime, _run_codex_memory_bootstrap
    from telegram_bot.core.usage import SNAPSHOT_TTL_SECONDS, UsageSnapshot


async def next_event(stream: AsyncIterator[AgentEvent]) -> AgentEvent:
    return await anext(stream)


class FakeClient:
    def __init__(
        self,
        server_request_handler: Callable[
            [CodexServerRequest], Awaitable[Mapping[str, Any]]
        ],
    ) -> None:
        self.server_request_handler = server_request_handler
        self.start_calls = 0
        self.close_calls = 0
        self.thread_start_calls: list[dict[str, Any]] = []
        self.thread_resume_calls: list[dict[str, Any]] = []
        self.thread_start_result: Any = {"thread": {"id": "thread-new"}}
        self.notifications: asyncio.Queue[CodexNotification | BaseException] = asyncio.Queue()
        self.model_result: Any = {"data": []}
        self.rate_limits_result: Any = {"rateLimits": {}}
        self.account_usage_result: Any = {"summary": {}}
        self.rate_limits_error: BaseException | None = None
        self.account_usage_error: BaseException | None = None
        self.rate_limits_calls = 0
        self.account_usage_calls = 0
        self.thread_pages: list[Any] = []
        self.thread_reads: dict[str, Any] = {}
        self.thread_read_calls: list[dict[str, Any]] = []
        self.thread_list_calls: list[dict[str, Any]] = []
        self.turn_start_calls: list[dict[str, Any]] = []
        self.interrupt_calls: list[tuple[str, str]] = []
        self.before_turn_response: list[CodexNotification] = []
        self.before_turn_server_requests: list[CodexServerRequest] = []
        self.server_request_tasks: list[asyncio.Task[Mapping[str, Any]]] = []
        self.turn_started = asyncio.Event()
        self.release_turn_start = asyncio.Event()
        self.release_turn_start.set()

    async def start(self) -> None:
        self.start_calls += 1

    async def thread_start(self, *, cwd: str, model: str | None = None) -> Any:
        self.thread_start_calls.append({"cwd": cwd, "model": model})
        return self.thread_start_result

    async def thread_resume(
        self,
        thread_id: str,
        *,
        cwd: str | None = None,
        model: str | None = None,
    ) -> Any:
        self.thread_resume_calls.append({"thread_id": thread_id, "cwd": cwd, "model": model})
        return {"thread": {"id": thread_id}}

    async def list_models(self, *, include_hidden: bool = False) -> Any:
        return self.model_result

    async def account_rate_limits(self) -> Any:
        self.rate_limits_calls += 1
        if self.rate_limits_error is not None:
            raise self.rate_limits_error
        return self.rate_limits_result

    async def account_usage(self) -> Any:
        self.account_usage_calls += 1
        if self.account_usage_error is not None:
            raise self.account_usage_error
        return self.account_usage_result

    async def thread_list(self, *, limit: int = 20, cursor: str | None = None) -> Any:
        self.thread_list_calls.append({"limit": limit, "cursor": cursor})
        return self.thread_pages.pop(0)

    async def thread_read(self, thread_id: str, *, include_turns: bool = True) -> Any:
        self.thread_read_calls.append(
            {"thread_id": thread_id, "include_turns": include_turns}
        )
        return self.thread_reads.get(thread_id)

    async def turn_start(
        self,
        thread_id: str,
        input_items: Sequence[Mapping[str, Any]],
        *,
        model: str | None = None,
        effort: str | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox_policy: Mapping[str, Any] | None = None,
    ) -> Any:
        turn_id = f"turn-{len(self.turn_start_calls) + 1}"
        self.turn_start_calls.append(
            {
                "thread_id": thread_id,
                "input": list(input_items),
                "model": model,
                "effort": effort,
                "approval_policy": approval_policy,
                "approvals_reviewer": approvals_reviewer,
                "sandbox_policy": dict(sandbox_policy) if sandbox_policy is not None else None,
                "turn_id": turn_id,
            }
        )
        self.turn_started.set()

        async def handle(request: CodexServerRequest) -> Mapping[str, Any]:
            return await self.server_request_handler(request)

        for request in self.before_turn_server_requests:
            self.server_request_tasks.append(asyncio.create_task(handle(request)))
            await asyncio.sleep(0)
        self.before_turn_server_requests.clear()
        for notification in self.before_turn_response:
            await self.notifications.put(notification)
            await asyncio.sleep(0)
        self.before_turn_response.clear()
        await self.release_turn_start.wait()
        return {"turn": {"id": turn_id}}

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> Any:
        self.interrupt_calls.append((thread_id, turn_id))
        return {"ok": True}

    async def next_notification(self) -> CodexNotification:
        value = await self.notifications.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self) -> None:
        self.close_calls += 1


class CodexRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clients: list[FakeClient] = []

        def factory(
            handler: Callable[[CodexServerRequest], Awaitable[Mapping[str, Any]]],
        ) -> FakeClient:
            client = FakeClient(handler)
            self.clients.append(client)
            return client

        self.runtime = CodexRuntime(client_factory=factory)

    async def asyncTearDown(self) -> None:
        await self.runtime.close()

    async def test_start_resume_and_model_list_use_one_initialized_client(self) -> None:
        runtime_contract: AgentRuntime = self.runtime
        new_session, resumed_session = await asyncio.gather(
            runtime_contract.start_or_resume(
                SessionRequest(working_directory="/workspace/new", model="codex-a")
            ),
            runtime_contract.start_or_resume(
                SessionRequest(
                    working_directory="/workspace/resume",
                    session_id="thread-old",
                    model="codex-b",
                )
            ),
        )
        client = self.clients[0]
        client.model_result = {
            "data": [
                {
                    "id": "codex-a",
                    "displayName": "Codex A",
                    "defaultReasoningEffort": "medium",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "low", "description": "Fast"},
                        {"reasoningEffort": "medium", "description": "Balanced"},
                        {"reasoningEffort": "high", "description": "Deep"},
                        {"reasoningEffort": "high", "description": "Duplicate"},
                        {"description": "Malformed"},
                    ],
                    "isDefault": True,
                },
                {"id": "", "displayName": "invalid"},
                {"id": "codex-b", "name": "Codex B"},
                {"id": 7, "displayName": "invalid"},
            ]
        }

        models: Sequence[ModelInfo] = await runtime_contract.list_models()

        self.assertEqual(len(self.clients), 1)
        self.assertEqual(client.start_calls, 1)
        self.assertEqual(new_session.session_id, "thread-new")
        self.assertEqual(resumed_session.session_id, "thread-old")
        self.assertEqual(
            client.thread_start_calls,
            [{"cwd": "/workspace/new", "model": "codex-a"}],
        )
        self.assertEqual(
            client.thread_resume_calls,
            [{"thread_id": "thread-old", "cwd": "/workspace/resume", "model": "codex-b"}],
        )
        self.assertEqual(
            models,
            (
                ModelInfo(
                    "codex-a",
                    "Codex A",
                    default_reasoning_effort="medium",
                    supported_reasoning_efforts=("low", "medium", "high"),
                    is_default=True,
                ),
                ModelInfo("codex-b", "Codex B"),
            ),
        )

    async def test_usage_reads_account_without_turn_and_isolates_exact_thread_cache(self) -> None:
        await self.runtime._ensure_started()
        client = self.clients[0]
        client.rate_limits_result = {
            "rateLimits": {
                "planType": "plus",
                "primary": {"usedPercent": 12, "windowDurationMins": 300},
            }
        }
        client.account_usage_result = {
            "summary": {"lifetimeTokens": 9000},
            "dailyUsageBuckets": [{"startDate": "2026-07-15", "tokens": 100}],
        }
        self.runtime._route_notification(
            CodexNotification(
                "thread/tokenUsage/updated",
                {
                    "threadId": "thread-a",
                    "turnId": "turn-a",
                    "tokenUsage": {
                        "last": {"totalTokens": 300},
                        "total": {
                            "inputTokens": 500,
                            "outputTokens": 100,
                            "reasoningOutputTokens": 0,
                            "cachedInputTokens": 0,
                            "totalTokens": 600,
                        },
                        "modelContextWindow": 200000,
                    },
                },
            )
        )

        matching = await self.runtime.get_usage("thread-a")
        other = await self.runtime.get_usage("thread-b")

        self.assertEqual(client.turn_start_calls, [])
        self.assertEqual(client.rate_limits_calls, 2)
        self.assertEqual(client.account_usage_calls, 2)
        self.assertEqual(matching.context_used, 300)
        self.assertEqual(matching.lifetime_tokens, 9000)
        self.assertIsNone(other.context_used)

    async def test_usage_sparse_updates_merge_and_read_errors_fail_safe(self) -> None:
        await self.runtime._ensure_started()
        client = self.clients[0]
        self.runtime._route_notification(
            CodexNotification(
                "account/rateLimits/updated",
                {
                    "rateLimits": {
                        "planType": "plus",
                        "limitName": "five hour",
                        "primary": {"usedPercent": 10},
                        "secondary": {"usedPercent": 20},
                    }
                },
            )
        )
        self.runtime._route_notification(
            CodexNotification(
                "account/rateLimits/updated",
                {
                    "rateLimits": {
                        "limitName": "five hour",
                        "primary": {"usedPercent": 35},
                    }
                },
            )
        )
        client.rate_limits_error = TimeoutError("private transport detail")
        client.account_usage_error = ConnectionError("private transport detail")

        usage = await self.runtime.get_usage(None)

        self.assertEqual(usage.plan_type, "plus")
        self.assertEqual(
            [(item.label, item.used_percent) for item in usage.windows],
            [("five hour primary", 35), ("five hour secondary", 20)],
        )

        self.runtime._account_rate_limits = UsageSnapshot(
            provider="codex",
            plan_type="stale-plan",
            observed_at=time.time() - SNAPSHOT_TTL_SECONDS - 1,
        )
        stale = await self.runtime.get_usage(None)
        self.assertIsNone(stale.plan_type)

    async def test_memory_bootstrap_runs_before_each_thread_boundary(self) -> None:
        clients: list[FakeClient] = []

        def factory(handler):
            client = FakeClient(handler)
            clients.append(client)
            return client

        observations: list[tuple[int, int, int]] = []

        async def bootstrap() -> None:
            client = clients[0]
            observations.append(
                (
                    client.start_calls,
                    len(client.thread_start_calls),
                    len(client.thread_resume_calls),
                )
            )

        runtime = CodexRuntime(client_factory=factory, memory_bootstrap=bootstrap)
        try:
            await runtime.start_or_resume(SessionRequest(working_directory="/new"))
            await runtime.start_or_resume(
                SessionRequest(working_directory="/resume", session_id="thread-old")
            )
        finally:
            await runtime.close()
        self.assertEqual(observations, [(0, 0, 0), (1, 1, 0)])
        self.assertEqual(clients[0].start_calls, 1)

    async def test_memory_bootstrap_failure_prevents_memoryless_thread_start(self) -> None:
        clients: list[FakeClient] = []

        def factory(handler):
            client = FakeClient(handler)
            clients.append(client)
            return client

        async def bootstrap() -> None:
            raise RuntimeError("codex memory bootstrap unavailable")

        runtime = CodexRuntime(client_factory=factory, memory_bootstrap=bootstrap)
        try:
            with self.assertRaisesRegex(RuntimeError, "memory bootstrap unavailable"):
                await runtime.start_or_resume(SessionRequest(working_directory="/blocked"))
        finally:
            await runtime.close()
        self.assertEqual(clients[0].start_calls, 0)
        self.assertEqual(clients[0].thread_start_calls, [])

    async def test_subprocess_bootstrap_accepts_last_ready_and_rejects_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ready = root / "ready-materializer"
            ready.write_text(
                "#!/usr/bin/env bash\n"
                'case "${1:-}" in materialize) exit 9;; status) exit 0;; *) exit 64;; esac\n',
                encoding="utf-8",
            )
            ready.chmod(0o700)
            await _run_codex_memory_bootstrap(str(ready), timeout_seconds=1.0)

            missing = root / "missing-materializer"
            missing.write_text(
                "#!/usr/bin/env bash\nexit 9\n",
                encoding="utf-8",
            )
            missing.chmod(0o700)
            with self.assertRaisesRegex(RuntimeError, "memory bootstrap unavailable"):
                await _run_codex_memory_bootstrap(str(missing), timeout_seconds=1.0)

    async def test_start_rejects_a_malformed_thread_identifier(self) -> None:
        self.clients[0].thread_start_result = {"thread": {"id": ""}}

        with self.assertRaisesRegex(RuntimeError, "invalid thread id"):
            await self.runtime.start_or_resume(SessionRequest(working_directory="/workspace"))

    async def test_session_list_is_bounded_and_read_exposes_only_visible_messages(self) -> None:
        from telegram_bot.core.agent_runtime import SessionHistoryMessage
        from telegram_bot.core.codex_app_server import (
            CodexThread,
            CodexThreadListPage,
            CodexThreadSummary,
        )

        client = self.clients[0]
        client.thread_pages = [
            CodexThreadListPage(
                data=(CodexThreadSummary("thread-1", "Title", "Preview", 42.0, "/work", "o3"),),
                next_cursor="cursor-2",
            ),
            CodexThreadListPage(
                data=(CodexThreadSummary("thread-2", None, None, None, None, None),),
                next_cursor="cursor-3",
            ),
        ]

        sessions = await self.runtime.list_sessions(limit=50, max_pages=2)

        self.assertEqual([session.id for session in sessions], ["thread-1", "thread-2"])
        self.assertEqual(client.thread_list_calls, [
            {"limit": 20, "cursor": None},
            {"limit": 20, "cursor": "cursor-2"},
        ])
        client.thread_reads["thread-1"] = CodexThread(
            id="thread-1",
            turns=(
                {"id": "turn-1", "createdAt": "2026-01-01T00:00:00Z", "items": (
                    {"type": "userMessage", "content": ({"type": "text", "text": "hello"},)},
                    {"type": "reasoning", "summary": ("secret",)},
                    {"type": "commandExecution", "command": "cat secret"},
                    {"type": "agentMessage", "text": "world"},
                )},
            ),
        )

        history = await self.runtime.read_session("thread-1", limit=5)

        self.assertEqual(history.session_id, "thread-1")
        self.assertEqual(history.messages, (
            SessionHistoryMessage("user", "hello", "2026-01-01T00:00:00Z"),
            SessionHistoryMessage("assistant", "world", "2026-01-01T00:00:00Z"),
        ))
        self.assertNotIn("secret", repr(history))
        self.assertNotIn("cat secret", repr(history))

    async def test_distill_snapshot_is_bounded_utf8_safe_and_thread_read_only(self) -> None:
        from telegram_bot.core.codex_app_server import CodexThread
        from telegram_bot.memory.distill_types import TranscriptBounds

        client = self.clients[0]
        client.thread_reads["thread-old"] = CodexThread(
            id="thread-old",
            turns=(
                {
                    "id": "turn-too-old",
                    "createdAt": "2026-07-01T00:00:00Z",
                    "items": (
                        {"type": "userMessage", "content": "old text"},
                    ),
                },
                {
                    "id": "turn-new",
                    "createdAt": "2026-07-14T05:59:30Z",
                    "items": (
                        {
                            "type": "userMessage",
                            "content": ({"type": "text", "text": "hello"},),
                        },
                        {
                            "type": "commandExecution",
                            "aggregatedOutput": "raw tool secret",
                        },
                        {"type": "agentMessage", "text": "🙂🙂🙂"},
                        {"type": "unknown", "text": "unknown secret"},
                    ),
                },
            ),
        )

        result = await self.runtime.read_session_snapshot(
            "thread-old",
            bounds=TranscriptBounds(
                max_turns=1,
                max_items=10,
                max_messages=2,
                max_bytes=13,
                max_message_bytes=8,
                max_age_seconds=3600,
            ),
            now=datetime(2026, 7, 14, 6, 0, tzinfo=timezone.utc),
        )

        self.assertEqual([message.role for message in result.messages], ["user", "assistant"])
        self.assertEqual([message.text for message in result.messages], ["hello", "🙂🙂"])
        self.assertEqual(result.byte_count, 13)
        self.assertEqual(result.last_turn_id, "turn-new")
        self.assertTrue(result.truncated)
        self.assertNotIn("secret", repr(result))
        self.assertEqual(
            client.thread_read_calls,
            [{"thread_id": "thread-old", "include_turns": True}],
        )
        self.assertEqual(client.thread_start_calls, [])
        self.assertEqual(client.thread_resume_calls, [])
        self.assertEqual(client.turn_start_calls, [])

    async def test_turn_maps_streamed_events_including_notifications_before_response(self) -> None:
        session = await self.runtime.start_or_resume(
            SessionRequest(
                working_directory="/workspace",
                effort="high",
                approval_policy="on-request",
                approvals_reviewer="auto_review",
                sandbox_policy={"type": "workspaceWrite", "networkAccess": False},
            )
        )
        client = self.clients[0]
        client.before_turn_response = [
            CodexNotification(
                "item/agentMessage/delta",
                {"threadId": "thread-new", "turnId": "turn-other", "delta": "wrong turn"},
            ),
            CodexNotification(
                "turn/completed",
                {
                    "threadId": "thread-new",
                    "turn": {"id": "turn-other", "status": "completed"},
                },
            ),
            CodexNotification(
                "item/agentMessage/delta",
                {"threadId": "thread-new", "turnId": "turn-1", "delta": "hello"},
            ),
            CodexNotification(
                "item/reasoning/textDelta",
                {"threadId": "thread-new", "turnId": "turn-1", "delta": "thinking"},
            ),
            CodexNotification(
                "item/started",
                {
                    "threadId": "thread-new",
                    "turnId": "turn-1",
                    "item": {"id": "message-1", "type": "agentMessage", "text": "hello"},
                },
            ),
            CodexNotification(
                "item/started",
                {
                    "threadId": "thread-new",
                    "turnId": "turn-1",
                    "item": {"id": "item-1", "type": "commandExecution", "command": "pwd"},
                },
            ),
            CodexNotification(
                "item/completed",
                {
                    "threadId": "thread-new",
                    "turnId": "turn-1",
                    "item": {
                        "id": "item-1",
                        "type": "commandExecution",
                        "command": "pwd",
                        "aggregatedOutput": "/workspace",
                        "exitCode": 0,
                        "status": "completed",
                    },
                },
            ),
            CodexNotification(
                "turn/completed",
                {
                    "threadId": "thread-new",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            ),
        ]

        events = [event async for event in session.send_turn("hello")]

        self.assertEqual(
            [event.kind for event in events],
            [
                "text_delta",
                "reasoning_delta",
                "tool_started",
                "tool_completed",
                "result",
                "completion",
            ],
        )
        self.assertEqual(cast(TextDeltaEvent, events[0]).text, "hello")
        self.assertEqual(cast(ReasoningDeltaEvent, events[1]).text, "thinking")
        self.assertEqual(cast(ToolStartedEvent, events[2]).tool_name, "commandExecution")
        self.assertEqual(cast(ToolCompletedEvent, events[3]).success, True)
        self.assertIsInstance(events[4], ResultEvent)
        self.assertIsInstance(events[5], CompletionEvent)
        self.assertEqual(
            client.turn_start_calls[0]["input"],
            [{"type": "text", "text": "hello"}],
        )
        self.assertEqual(client.turn_start_calls[0]["effort"], "high")
        self.assertEqual(client.turn_start_calls[0]["approval_policy"], "on-request")
        self.assertEqual(client.turn_start_calls[0]["approvals_reviewer"], "auto_review")
        self.assertEqual(
            client.turn_start_calls[0]["sandbox_policy"],
            {"type": "workspaceWrite", "networkAccess": False},
        )

    async def test_completed_agent_message_emits_semantic_boundary(self) -> None:
        # A completed agentMessage is a provider-neutral lifecycle boundary, not
        # text. Consumers decide whether it is an interim or terminal message.
        session = await self.runtime.start_or_resume(
            SessionRequest(
                working_directory="/workspace",
                effort="high",
                approval_policy="on-request",
                approvals_reviewer="auto_review",
                sandbox_policy={"type": "workspaceWrite", "networkAccess": False},
            )
        )
        client = self.clients[0]
        client.before_turn_response = [
            CodexNotification(
                "item/agentMessage/delta",
                {"threadId": "thread-new", "turnId": "turn-1", "delta": "first part."},
            ),
            CodexNotification(
                "item/completed",
                {
                    "threadId": "thread-new",
                    "turnId": "turn-1",
                    "item": {"id": "message-1", "type": "agentMessage", "text": "first part."},
                },
            ),
            CodexNotification(
                "item/agentMessage/delta",
                {"threadId": "thread-new", "turnId": "turn-1", "delta": "second part."},
            ),
            CodexNotification(
                "item/completed",
                {
                    "threadId": "thread-new",
                    "turnId": "turn-1",
                    "item": {"id": "message-2", "type": "agentMessage", "text": "second part."},
                },
            ),
            CodexNotification(
                "turn/completed",
                {"threadId": "thread-new", "turn": {"id": "turn-1", "status": "completed"}},
            ),
        ]

        events = [event async for event in session.send_turn("hi")]
        self.assertEqual(
            [event.kind for event in events],
            [
                "text_delta",
                "message_completed",
                "text_delta",
                "message_completed",
                "result",
                "completion",
            ],
        )
        boundaries = [event for event in events if isinstance(event, MessageCompletedEvent)]
        self.assertEqual(len(boundaries), 2)

    async def test_agent_message_boundary_never_leads_or_doubles(self) -> None:
        # A completed agentMessage with no preceding text emits nothing; a second
        # consecutive completion without new text does not double the boundary.
        session = await self.runtime.start_or_resume(
            SessionRequest(
                working_directory="/workspace",
                effort="high",
                approval_policy="on-request",
                approvals_reviewer="auto_review",
                sandbox_policy={"type": "workspaceWrite", "networkAccess": False},
            )
        )
        client = self.clients[0]
        client.before_turn_response = [
            CodexNotification(
                "item/completed",  # completes before any text → must NOT lead with "\n\n"
                {
                    "threadId": "thread-new",
                    "turnId": "turn-1",
                    "item": {"id": "message-0", "type": "agentMessage", "text": ""},
                },
            ),
            CodexNotification(
                "item/agentMessage/delta",
                {"threadId": "thread-new", "turnId": "turn-1", "delta": "only"},
            ),
            CodexNotification(
                "item/completed",
                {
                    "threadId": "thread-new",
                    "turnId": "turn-1",
                    "item": {"id": "message-1", "type": "agentMessage", "text": "only"},
                },
            ),
            CodexNotification(
                "item/completed",  # consecutive completion, no new text → no double "\n\n"
                {
                    "threadId": "thread-new",
                    "turnId": "turn-1",
                    "item": {"id": "message-1", "type": "agentMessage", "text": "only"},
                },
            ),
            CodexNotification(
                "turn/completed",
                {"threadId": "thread-new", "turn": {"id": "turn-1", "status": "completed"}},
            ),
        ]

        events = [event async for event in session.send_turn("hi")]
        self.assertEqual(
            [event.kind for event in events],
            ["text_delta", "message_completed", "result", "completion"],
        )

    async def test_approval_before_turn_start_response_routes_to_handler(self) -> None:
        session = await self.runtime.start_or_resume(
            SessionRequest(
                working_directory="/workspace",
                approval_policy="untrusted",
            )
        )
        client = self.clients[0]
        client.before_turn_server_requests = [
            CodexServerRequest(
                "approval-early",
                "item/commandExecution/requestApproval",
                {
                    "threadId": "thread-new",
                    "turnId": "turn-1",
                    "itemId": "item-1",
                },
            )
        ]
        seen: list[str] = []

        async def allow(request: ApprovalRequestEvent) -> ApprovalDecision:
            seen.append(request.request_id)
            return ApprovalDecision.ALLOW

        stream = session.send_turn("edit", approval_handler=allow)
        consumer = asyncio.create_task(next_event(stream))
        try:
            while not client.server_request_tasks:
                await asyncio.sleep(0)
            response = await asyncio.wait_for(client.server_request_tasks[0], timeout=0.2)
            self.assertEqual(response, {"result": {"decision": "accept"}})
            self.assertEqual(seen, ["approval-early"])
            event = await asyncio.wait_for(consumer, timeout=0.2)
            self.assertIsInstance(event, ApprovalRequestEvent)
            self.assertEqual(cast(ApprovalRequestEvent, event).request_id, "approval-early")
        finally:
            consumer.cancel()
            with suppress(asyncio.CancelledError):
                await consumer

    async def test_mismatched_approval_before_turn_start_response_is_declined(
        self,
    ) -> None:
        session = await self.runtime.start_or_resume(
            SessionRequest(
                working_directory="/workspace",
                approval_policy="untrusted",
            )
        )
        client = self.clients[0]
        client.before_turn_server_requests = [
            CodexServerRequest(
                "approval-mismatch",
                "item/commandExecution/requestApproval",
                {
                    "threadId": "thread-new",
                    "turnId": "turn-other",
                    "itemId": "item-1",
                },
            )
        ]
        seen: list[str] = []

        async def allow(request: ApprovalRequestEvent) -> ApprovalDecision:
            seen.append(request.request_id)
            return ApprovalDecision.ALLOW

        stream = session.send_turn("edit", approval_handler=allow)
        consumer = asyncio.create_task(next_event(stream))
        try:
            while not client.server_request_tasks:
                await asyncio.sleep(0)
            response = await asyncio.wait_for(client.server_request_tasks[0], timeout=0.2)
            self.assertEqual(response, {"result": {"decision": "decline"}})
            self.assertEqual(seen, [])
        finally:
            consumer.cancel()
            with suppress(asyncio.CancelledError):
                await consumer

    async def test_approval_routes_exact_turn_and_fails_closed(self) -> None:
        session = await self.runtime.start_or_resume(SessionRequest(working_directory="/workspace"))
        client = self.clients[0]
        seen: list[ApprovalRequestEvent] = []

        async def allow(request: ApprovalRequestEvent) -> ApprovalDecision:
            seen.append(request)
            return ApprovalDecision.ALLOW

        stream = session.send_turn("edit", approval_handler=allow)
        first_event: asyncio.Task[AgentEvent] = asyncio.create_task(next_event(stream))
        await client.turn_started.wait()
        await asyncio.sleep(0)

        allowed = await client.server_request_handler(
            CodexServerRequest(
                "approval-1",
                "item/commandExecution/requestApproval",
                {
                    "threadId": "thread-new",
                    "turnId": "turn-1",
                    "itemId": "item-1",
                    "command": "pwd",
                },
            )
        )

        self.assertEqual(allowed, {"result": {"decision": "accept"}})
        self.assertEqual(await first_event, seen[0])
        self.assertEqual(seen[0].request_id, "approval-1")

        allowed_permissions = await client.server_request_handler(
            CodexServerRequest(
                "approval-2",
                "item/permissions/requestApproval",
                {
                    "threadId": "thread-new",
                    "turnId": "turn-1",
                    "permissions": {"network": True},
                },
            )
        )
        self.assertEqual(
            allowed_permissions,
            {"result": {"permissions": {"network": True}, "scope": "turn"}},
        )

        denied_cases = (
            CodexServerRequest(
                "missing-turn",
                "item/fileChange/requestApproval",
                {"threadId": "thread-new", "turnId": "other", "itemId": "item-2"},
            ),
            CodexServerRequest(
                "missing-thread",
                "item/commandExecution/requestApproval",
                {"threadId": "other", "turnId": "turn-1", "itemId": "item-3"},
            ),
        )
        for request in denied_cases:
            self.assertEqual(
                await client.server_request_handler(request),
                {"result": {"decision": "decline"}},
            )
        unknown = await client.server_request_handler(
            CodexServerRequest(
                "unknown",
                "unknown/approval",
                {"threadId": "thread-new", "turnId": "turn-1"},
            )
        )
        self.assertEqual(cast(Mapping[str, Any], unknown["error"])["code"], -32601)

        await client.notifications.put(
            CodexNotification(
                "turn/completed",
                {"threadId": "thread-new", "turn": {"id": "turn-1", "status": "completed"}},
            )
        )
        await asyncio.sleep(0)
        finished_denied = await client.server_request_handler(
            CodexServerRequest(
                "late",
                "item/commandExecution/requestApproval",
                {"threadId": "thread-new", "turnId": "turn-1", "itemId": "late"},
            )
        )
        self.assertEqual(finished_denied, {"result": {"decision": "decline"}})
        await cast(Any, stream).aclose()

    async def test_sessions_run_independently_while_each_session_serializes_and_interrupts_exactly(self) -> None:
        first = await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace/a", session_id="thread-a")
        )
        second = await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace/b", session_id="thread-b")
        )
        client = self.clients[0]
        await second.interrupt()

        async def collect(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
            return [event async for event in stream]

        first_turn = asyncio.create_task(collect(first.send_turn("first")))
        queued_turn = asyncio.create_task(collect(first.send_turn("queued")))
        while len(client.turn_start_calls) < 1:
            await asyncio.sleep(0)
        self.assertEqual(len(client.turn_start_calls), 1)

        other_turn = asyncio.create_task(collect(second.send_turn("parallel")))
        while len(client.turn_start_calls) < 2:
            await asyncio.sleep(0)
        self.assertEqual(
            {call["thread_id"] for call in client.turn_start_calls},
            {"thread-a", "thread-b"},
        )

        await first.interrupt()
        self.assertEqual(client.interrupt_calls, [("thread-a", "turn-1")])
        await client.notifications.put(
            CodexNotification(
                "turn/completed",
                {"threadId": "thread-a", "turn": {"id": "turn-1", "status": "completed"}},
            )
        )
        while len(client.turn_start_calls) < 3:
            await asyncio.sleep(0)
        self.assertEqual(client.turn_start_calls[2]["thread_id"], "thread-a")
        await client.notifications.put(
            CodexNotification(
                "turn/completed",
                {"threadId": "thread-b", "turn": {"id": "turn-2", "status": "completed"}},
            )
        )
        await client.notifications.put(
            CodexNotification(
                "turn/completed",
                {"threadId": "thread-a", "turn": {"id": "turn-3", "status": "completed"}},
            )
        )

        results = await asyncio.wait_for(
            asyncio.gather(first_turn, queued_turn, other_turn), timeout=0.2
        )
        self.assertTrue(all(events[-1].kind == "completion" for events in results))

    async def test_two_session_objects_for_the_same_thread_share_turn_serialization(self) -> None:
        first = await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace", session_id="shared-thread")
        )
        duplicate = await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace", session_id="shared-thread")
        )
        client = self.clients[0]

        async def collect(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
            return [event async for event in stream]

        first_task = asyncio.create_task(collect(first.send_turn("first")))
        duplicate_task = asyncio.create_task(collect(duplicate.send_turn("second")))
        while len(client.turn_start_calls) < 1:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(len(client.turn_start_calls), 1)
        await client.notifications.put(
            CodexNotification(
                "turn/completed",
                {"threadId": "shared-thread", "turn": {"id": "turn-1", "status": "completed"}},
            )
        )
        while len(client.turn_start_calls) < 2:
            await asyncio.sleep(0)
        await client.notifications.put(
            CodexNotification(
                "turn/completed",
                {"threadId": "shared-thread", "turn": {"id": "turn-2", "status": "completed"}},
            )
        )
        await asyncio.wait_for(asyncio.gather(first_task, duplicate_task), timeout=0.2)

    async def test_dispatcher_failure_terminates_active_stream_with_error(self) -> None:
        session = await self.runtime.start_or_resume(SessionRequest(working_directory="/workspace"))
        client = self.clients[0]

        async def collect() -> list[AgentEvent]:
            return [event async for event in session.send_turn("hello")]

        pending = asyncio.create_task(collect())
        await client.turn_started.wait()
        await client.notifications.put(ConnectionError())

        events = await asyncio.wait_for(pending, timeout=0.2)
        self.assertEqual(events[-1].kind, "error")
        self.assertEqual(cast(Any, events[-1]).code, "codex_connection_failed")

    async def test_unknown_notifications_are_ignored_and_terminal_failures_are_deterministic(self) -> None:
        failed_session = await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace/fail", session_id="thread-fail")
        )
        interrupted_session = await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace/stop", session_id="thread-stop")
        )
        client = self.clients[0]

        async def collect(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
            return [event async for event in stream]

        failed = asyncio.create_task(collect(failed_session.send_turn("fail")))
        interrupted = asyncio.create_task(collect(interrupted_session.send_turn("stop")))
        while len(client.turn_start_calls) < 2:
            await asyncio.sleep(0)
        await client.notifications.put(
            CodexNotification(
                "future/unknown",
                {"threadId": "thread-fail", "turnId": "turn-1", "value": "ignored"},
            )
        )
        await client.notifications.put(
            CodexNotification(
                "turn/completed",
                {
                    "threadId": "thread-fail",
                    "turn": {"id": "turn-1", "status": "failed", "error": "bad request"},
                },
            )
        )
        await client.notifications.put(
            CodexNotification(
                "turn/completed",
                {"threadId": "thread-stop", "turn": {"id": "turn-2", "status": "interrupted"}},
            )
        )

        failed_events, interrupted_events = await asyncio.wait_for(
            asyncio.gather(failed, interrupted), timeout=0.2
        )
        self.assertEqual(cast(Any, failed_events[-1]).code, "codex_turn_failed")
        self.assertEqual(cast(Any, interrupted_events[-1]).code, "interrupted")

    async def test_approval_missing_handler_callback_error_and_permissions_are_fail_closed(self) -> None:
        session = await self.runtime.start_or_resume(SessionRequest(working_directory="/workspace"))
        client = self.clients[0]
        stream = session.send_turn("edit")
        approval_event: asyncio.Task[AgentEvent] = asyncio.create_task(next_event(stream))
        await client.turn_started.wait()
        await asyncio.sleep(0)

        denied = await client.server_request_handler(
            CodexServerRequest(
                1,
                "item/commandExecution/requestApproval",
                {"threadId": "thread-new", "turnId": "turn-1", "itemId": "item-1"},
            )
        )
        self.assertEqual(denied, {"result": {"decision": "decline"}})
        self.assertIsInstance(await approval_event, ApprovalRequestEvent)

        permissions = await client.server_request_handler(
            CodexServerRequest(
                2,
                "item/permissions/requestApproval",
                {
                    "threadId": "thread-new",
                    "turnId": "turn-1",
                    "permissions": {"network": True},
                },
            )
        )
        self.assertEqual(permissions, {"result": {"permissions": {}, "scope": "turn"}})
        await cast(Any, stream).aclose()

        async def fail(_request: ApprovalRequestEvent) -> ApprovalDecision:
            raise RuntimeError("callback failed")

        second_session = await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace/other", session_id="thread-other")
        )
        failed_stream = second_session.send_turn("edit", approval_handler=fail)
        failed_event: asyncio.Task[AgentEvent] = asyncio.create_task(next_event(failed_stream))
        while len(client.turn_start_calls) < 2:
            await asyncio.sleep(0)
        callback_denied = await client.server_request_handler(
            CodexServerRequest(
                3,
                "item/fileChange/requestApproval",
                {"threadId": "thread-other", "turnId": "turn-2", "itemId": "item-2"},
            )
        )
        self.assertEqual(callback_denied, {"result": {"decision": "decline"}})
        self.assertIsInstance(await failed_event, ApprovalRequestEvent)
        await cast(Any, failed_stream).aclose()


if __name__ == "__main__":
    unittest.main()
