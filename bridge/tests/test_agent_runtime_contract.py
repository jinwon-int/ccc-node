"""Provider-neutral agent runtime contract tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from typing import cast
import unittest

from telegram_bot.core.agent_runtime import (
    AgentEvent,
    AgentRuntime,
    AgentSession,
    ApprovalDecision,
    ApprovalHandler,
    ApprovalRequestEvent,
    CompletionEvent,
    ErrorEvent,
    ModelInfo,
    ReasoningDeltaEvent,
    ResultEvent,
    SessionRequest,
    TextDeltaEvent,
    deny_approval,
)


class FakeSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.interrupted = False

    async def send_turn(
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
        decision = await approval_handler(request)

        async def events() -> AsyncIterator[AgentEvent]:
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
        self.assertIsInstance(runtime, AgentRuntime)

        session = await runtime.start_or_resume(
            SessionRequest(working_directory="/workspace", session_id="resume-me")
        )
        self.assertIsInstance(session, AgentSession)
        self.assertEqual(session.session_id, "resume-me")
        self.assertEqual((await runtime.list_models())[0].id, "fake-model")

        async def allow(_request: ApprovalRequestEvent) -> ApprovalDecision:
            return ApprovalDecision.ALLOW

        stream = await session.send_turn("hello", approval_handler=allow)
        events = [event async for event in stream]

        self.assertEqual(
            [event.kind for event in events],
            ["text_delta", "reasoning_delta", "approval_request", "completion", "result"],
        )
        await session.interrupt()
        self.assertTrue(cast(FakeSession, session).interrupted)

    async def test_approval_is_explicit_and_defaults_to_deny(self) -> None:
        session = FakeSession("session")

        stream = await session.send_turn("hello")
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
            lambda: ErrorEvent(code="", message="failed"),
        )

        for factory in invalid_factories:
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()


if __name__ == "__main__":
    unittest.main()
