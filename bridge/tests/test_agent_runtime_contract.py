"""Provider-neutral agent runtime contract tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast
import unittest

if TYPE_CHECKING:
    from core.agent_runtime import (
        AgentEvent,
        AgentRuntime,
        AgentSession,
        ApprovalDecision,
        ApprovalHandler,
        ApprovalRequestEvent,
        CompletionEvent,
        ErrorEvent,
        JsonValue,
        ModelInfo,
        ReasoningDeltaEvent,
        ResultEvent,
        SessionRequest,
        TextDeltaEvent,
        ToolCompletedEvent,
        ToolStartedEvent,
        deny_approval,
    )
else:
    from telegram_bot.core.agent_runtime import (
        AgentEvent,
        AgentRuntime,
        AgentSession,
        ApprovalDecision,
        ApprovalHandler,
        ApprovalRequestEvent,
        CompletionEvent,
        ErrorEvent,
        JsonValue,
        ModelInfo,
        ReasoningDeltaEvent,
        ResultEvent,
        SessionRequest,
        TextDeltaEvent,
        ToolCompletedEvent,
        ToolStartedEvent,
        deny_approval,
    )


from telegram_bot.core.agent_runtime import (
    SessionHistory,
    SessionHistoryMessage,
    SessionSummary,
)


class FakeSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.interrupted = False

    def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        request = ApprovalRequestEvent(
            request_id="approval-1",
            action="write_file",
            arguments={"path": "notes.txt"},
            description="Write notes.txt",
        )
        async def events() -> AsyncIterator[AgentEvent]:
            decision = await approval_handler(request)
            yield TextDeltaEvent(text=message)
            yield ReasoningDeltaEvent(text="checking approval")
            yield request
            if decision is ApprovalDecision.ALLOW:
                yield CompletionEvent(stop_reason="end_turn")
                yield ResultEvent(result={"status": "written"})
            else:
                yield ErrorEvent(code="approval_denied", message="Approval denied")

        return events()

    async def interrupt(self) -> None:
        self.interrupted = True


class FakeRuntime:
    async def start_or_resume(self, request: SessionRequest) -> AgentSession:
        return FakeSession(request.session_id or "new-session")

    async def list_models(self) -> Sequence[ModelInfo]:
        return (ModelInfo(id="fake-model", display_name="Fake model"),)


# Static type checkers must accept these structural implementations.
_fake_session_conforms: AgentSession = FakeSession("typed-session")
_fake_runtime_conforms: AgentRuntime = FakeRuntime()


class AgentRuntimeContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_fake_runtime_structurally_conforms_and_streams_all_event_kinds(self) -> None:
        runtime = FakeRuntime()
        session = await runtime.start_or_resume(
            SessionRequest(working_directory="/workspace", session_id="resume-me")
        )
        self.assertEqual(session.session_id, "resume-me")
        self.assertEqual((await runtime.list_models())[0].id, "fake-model")

        async def allow(_request: ApprovalRequestEvent) -> ApprovalDecision:
            return ApprovalDecision.ALLOW

        stream = session.send_turn("hello", approval_handler=allow)
        events = [event async for event in stream]

        self.assertEqual(
            [event.kind for event in events],
            ["text_delta", "reasoning_delta", "approval_request", "completion", "result"],
        )
        await session.interrupt()
        self.assertTrue(cast(FakeSession, session).interrupted)

    async def test_approval_is_explicit_and_defaults_to_deny(self) -> None:
        session = FakeSession("session")

        stream = session.send_turn("hello")
        events = [event async for event in stream]

        self.assertIsInstance(events[-1], ErrorEvent)
        self.assertEqual(cast(ErrorEvent, events[-1]).code, "approval_denied")
        request = cast(ApprovalRequestEvent, events[-2])
        self.assertEqual(request.arguments, {"path": "notes.txt"})

    async def test_normalized_events_enforce_non_empty_required_fields(self) -> None:
        invalid_factories: tuple[Callable[[], object], ...] = (
            lambda: TextDeltaEvent(text=""),
            lambda: ReasoningDeltaEvent(text=""),
            lambda: ApprovalRequestEvent(
                request_id="", action="write_file", arguments={}, description="Write a file"
            ),
            lambda: ApprovalRequestEvent(
                request_id="approval", action="", arguments={}, description="Write a file"
            ),
            lambda: ApprovalRequestEvent(
                request_id="approval", action="write_file", arguments={}, description=""
            ),
            lambda: CompletionEvent(stop_reason=""),
            lambda: ErrorEvent(code="", message="failed"),
            lambda: ErrorEvent(code="failed", message=""),
            lambda: SessionRequest(working_directory=""),
            lambda: SessionRequest(working_directory="/workspace", session_id=""),
            lambda: SessionRequest(working_directory="/workspace", model=""),
            lambda: SessionRequest(working_directory="/workspace", effort=""),
            lambda: SessionRequest(working_directory="/workspace", approvals_reviewer=""),
            lambda: SessionRequest(working_directory="/workspace", sandbox_policy={}),
            lambda: ModelInfo(id="", display_name="Fake model"),
            lambda: ModelInfo(id="fake", display_name=""),
        )

        for factory in invalid_factories:
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()

    async def test_event_payloads_are_recursively_immutable_snapshots(self) -> None:
        arguments: dict[str, JsonValue] = {"path": "notes.txt", "tags": ["a"]}
        request = ApprovalRequestEvent(
            request_id="approval-1",
            action="write_file",
            arguments=arguments,
            description="Write notes.txt",
        )
        result_source: dict[str, JsonValue] = {"items": [{"value": 1}]}
        result = ResultEvent(result=result_source)

        arguments["path"] = "changed.txt"
        cast(list[str], arguments["tags"]).append("b")
        cast(list[dict[str, int]], result_source["items"])[0]["value"] = 2

        self.assertEqual(request.arguments["path"], "notes.txt")
        self.assertEqual(request.arguments["tags"], ("a",))
        frozen_result = cast(Mapping[str, object], result.result)
        self.assertEqual(cast(tuple[Mapping[str, int], ...], frozen_result["items"])[0]["value"], 1)
        with self.assertRaises(TypeError):
            cast(dict[str, object], request.arguments)["path"] = "forbidden"

    async def test_session_request_sandbox_policy_is_recursively_immutable_snapshot(
        self,
    ) -> None:
        sandbox: dict[str, JsonValue] = {
            "type": "workspaceWrite",
            "networkAccess": False,
            "writableRoots": ["/workspace"],
        }
        request = SessionRequest(
            working_directory="/workspace",
            sandbox_policy=sandbox,
        )

        sandbox["networkAccess"] = True
        cast(list[str], sandbox["writableRoots"]).append("/tmp")

        assert request.sandbox_policy is not None
        self.assertFalse(request.sandbox_policy["networkAccess"])
        self.assertEqual(request.sandbox_policy["writableRoots"], ("/workspace",))
        with self.assertRaises(TypeError):
            cast(dict[str, JsonValue], request.sandbox_policy)["networkAccess"] = True

    async def test_tool_lifecycle_events_are_typed_immutable_snapshots(self) -> None:
        arguments: dict[str, JsonValue] = {"command": "pwd", "paths": ["."]}
        output: dict[str, JsonValue] = {"exitCode": 0, "lines": ["/workspace"]}

        started = ToolStartedEvent(tool_call_id="item-1", tool_name="command", arguments=arguments)
        completed = ToolCompletedEvent(
            tool_call_id="item-1", tool_name="command", result=output, success=True
        )
        cast(list[str], arguments["paths"]).append("changed")
        cast(list[str], output["lines"]).append("changed")

        self.assertEqual(started.kind, "tool_started")
        self.assertEqual(started.arguments["paths"], (".",))
        self.assertEqual(completed.kind, "tool_completed")
        self.assertEqual(cast(Mapping[str, object], completed.result)["lines"], ("/workspace",))


    async def test_session_browsing_values_are_immutable_and_validate_required_fields(self) -> None:
        summary = SessionSummary(
            id="thread-1",
            title="A title",
            preview="hello",
            updated_at=123.0,
            cwd="/workspace",
            model="codex-test",
        )
        message = SessionHistoryMessage(
            role="user", content="hello", timestamp="2026-01-01T00:00:00Z"
        )
        history = SessionHistory(session_id="thread-1", messages=[message])

        self.assertEqual(summary.id, "thread-1")
        self.assertEqual(history.messages, (message,))
        with self.assertRaises((AttributeError, TypeError)):
            cast(Any, history.messages).append(message)
        with self.assertRaises(ValueError):
            SessionSummary(id="")
        with self.assertRaises(ValueError):
            SessionHistoryMessage(role="tool", content="hidden")
        with self.assertRaises(ValueError):
            SessionHistory(session_id="", messages=())


if __name__ == "__main__":
    unittest.main()
