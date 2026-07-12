"""Deterministic tests for the provider-neutral Codex runtime adapter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast
import unittest

if TYPE_CHECKING:
    from core.agent_runtime import (
        AgentEvent,
        AgentRuntime,
        ApprovalDecision,
        ApprovalRequestEvent,
        CompletionEvent,
        ModelInfo,
        ReasoningDeltaEvent,
        ResultEvent,
        SessionRequest,
        TextDeltaEvent,
        ToolCompletedEvent,
        ToolStartedEvent,
    )
    from core.codex_app_server import CodexNotification, CodexServerRequest
    from core.codex_runtime import CodexRuntime
else:
    from telegram_bot.core.agent_runtime import (
        AgentEvent,
        AgentRuntime,
        ApprovalDecision,
        ApprovalRequestEvent,
        CompletionEvent,
        ModelInfo,
        ReasoningDeltaEvent,
        ResultEvent,
        SessionRequest,
        TextDeltaEvent,
        ToolCompletedEvent,
        ToolStartedEvent,
    )
    from telegram_bot.core.codex_app_server import CodexNotification, CodexServerRequest
    from telegram_bot.core.codex_runtime import CodexRuntime


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
        self.thread_pages: list[Any] = []
        self.thread_reads: dict[str, Any] = {}
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

    async def thread_list(self, *, limit: int = 20, cursor: str | None = None) -> Any:
        self.thread_list_calls.append({"limit": limit, "cursor": cursor})
        return self.thread_pages.pop(0)

    async def thread_read(self, thread_id: str, *, include_turns: bool = True) -> Any:
        return self.thread_reads.get(thread_id)

    async def turn_start(
        self,
        thread_id: str,
        input_items: Sequence[Mapping[str, Any]],
        *,
        model: str | None = None,
        effort: str | None = None,
        approval_policy: str | None = None,
    ) -> Any:
        turn_id = f"turn-{len(self.turn_start_calls) + 1}"
        self.turn_start_calls.append(
            {
                "thread_id": thread_id,
                "input": list(input_items),
                "model": model,
                "effort": effort,
                "approval_policy": approval_policy,
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

    async def test_turn_maps_streamed_events_including_notifications_before_response(self) -> None:
        session = await self.runtime.start_or_resume(
            SessionRequest(
                working_directory="/workspace",
                effort="high",
                approval_policy="untrusted",
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
        self.assertEqual(client.turn_start_calls[0]["approval_policy"], "untrusted")

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
