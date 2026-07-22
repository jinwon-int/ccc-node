"""Between-turns (unsolicited) frame handling for the Claude runtime adapter.

Assistant output produced outside an active turn — for example the CLI
autonomously continuing after a harness background-task notification — must
be delivered (the retired direct SDK path's unsolicited machinery pinned the
same behavior).  ``ClaudeRuntime`` carries it via the optional
``set_unsolicited_handler`` seam: buffer assistant text between
turns, deliver once on the terminal ResultMessage, keep ownership of an
in-flight autonomous turn when a user turn arrives, and never route mid-turn
frames to the unsolicited handler.

Note on the module name: like ``test_runtime_conformance`` this module drives
the real ``ClaudeRuntime`` over real ``claude_agent_sdk`` frame types, so it
must collect AFTER the project_chat modules that inject spec-less SDK stubs
and after ``test_runtime_conformance``'s purge/re-import — otherwise
``core.claude_runtime`` would bind a different import generation of the SDK
classes than the frames the fakes emit, breaking isinstance routing.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
import sys
import unittest
import uuid


def _purge_injected_sdk_stubs() -> None:
    """Drop spec-less ``claude_agent_sdk`` stubs left by earlier test modules.

    Same guard as ``test_runtime_conformance``: some project_chat test modules
    inject bare stub modules for ``claude_agent_sdk`` and never restore the
    real package.  This module drives the real ``ClaudeRuntime`` over real SDK
    frame types, so spec-less entries must be evicted before importing.
    """

    for name in [
        module_name
        for module_name in sys.modules
        if module_name == "claude_agent_sdk" or module_name.startswith("claude_agent_sdk.")
    ]:
        if getattr(sys.modules[name], "__spec__", None) is None:
            del sys.modules[name]


_purge_injected_sdk_stubs()

from claude_agent_sdk import (  # noqa: E402 -- must follow the stub purge above
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
)

from telegram_bot.core.agent_runtime import (  # noqa: E402
    AgentEvent,
    CompletionEvent,
    ErrorEvent,
    SessionRequest,
    TextDeltaEvent,
)
from telegram_bot.core.claude_runtime import ClaudeRuntime, ClaudeSession  # noqa: E402


class ManualClaudeSdkClient:
    """Fake SDK client with manual frame emission plus scripted turns.

    ``query`` consumes the next queued script: ``"answer"`` (the default)
    emits one assistant+result pair for the turn, ``"hang"`` emits nothing so
    the test controls every subsequent frame by hand.
    """

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.options = options
        self.session_id = options.resume or "claude-unsolicited-session"
        self.queries: list[str] = []
        self.turn_scripts: deque[str] = deque()
        self.interrupts = 0
        self._messages: asyncio.Queue[Message | None] = asyncio.Queue()

    # -- manual frame emission ---------------------------------------------

    def emit(self, message: Message) -> None:
        self._messages.put_nowait(message)

    def emit_stream_delta(self, text: str) -> None:
        self.emit(
            StreamEvent(
                uuid=str(uuid.uuid4()),
                session_id=self.session_id,
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )

    def emit_assistant(self, text: str) -> None:
        self.emit(
            AssistantMessage(
                content=[TextBlock(text=text)],
                model="claude-test-model",
                session_id=self.session_id,
            )
        )

    def emit_result(
        self,
        *,
        result: str | None = None,
        is_error: bool = False,
        usage: dict[str, int] | None = None,
        total_cost_usd: float | None = None,
    ) -> None:
        self.emit(
            ResultMessage(
                subtype="error_during_execution" if is_error else "success",
                duration_ms=5,
                duration_api_ms=3,
                is_error=is_error,
                num_turns=1,
                session_id=self.session_id,
                result=result,
                usage=usage,
                total_cost_usd=total_cost_usd,
            )
        )

    def emit_rate_limit(self, *, utilization: float = 0.5) -> None:
        self.emit(
            RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="allowed_warning",
                    resets_at=1_900_000_000,
                    rate_limit_type="five_hour",
                    utilization=utilization,
                ),
                uuid=str(uuid.uuid4()),
                session_id=self.session_id,
            )
        )

    def pending_frames(self) -> int:
        return self._messages.qsize()

    # -- SdkClient protocol ------------------------------------------------

    async def connect(self) -> None:
        self.emit(
            SystemMessage(
                subtype="init",
                data={"session_id": self.session_id, "cwd": "/workspace"},
            )
        )

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        script = self.turn_scripts.popleft() if self.turn_scripts else "answer"
        if script == "answer":
            self.emit_assistant("turn answer")
            self.emit_result(result="turn answer")
        elif script == "hang":
            pass  # the test emits this turn's frames (if any) by hand

    async def receive_messages(self) -> AsyncIterator[Message]:
        while True:
            message = await self._messages.get()
            if message is None:
                return
            yield message

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def disconnect(self) -> None:
        self._messages.put_nowait(None)


async def _collect(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in stream]


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was not reached in time")
        await asyncio.sleep(0.001)


class ClaudeRuntimeUnsolicitedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clients: list[ManualClaudeSdkClient] = []

        def factory(options: ClaudeAgentOptions) -> ManualClaudeSdkClient:
            client = ManualClaudeSdkClient(options)
            self.clients.append(client)
            return client

        self.runtime = ClaudeRuntime(sdk_client_factory=factory)
        self.addAsyncCleanup(self.runtime.close)
        self.delivered: list[tuple[str, str | None]] = []
        self.delivered_event = asyncio.Event()

    async def _handler(self, text: str, session_id: str | None) -> None:
        self.delivered.append((text, session_id))
        self.delivered_event.set()

    async def _start_session(self) -> tuple[ClaudeSession, ManualClaudeSdkClient]:
        session = await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace")
        )
        return session, self.clients[-1]

    async def _drain(self, client: ManualClaudeSdkClient) -> None:
        """Wait until the reader task consumed every emitted frame."""

        await _wait_until(lambda: client.pending_frames() == 0)
        # The final frame may still be mid-routing after the queue empties;
        # yield a few times so its (synchronous) routing completes.
        for _ in range(5):
            await asyncio.sleep(0)

    # -- between-turns delivery --------------------------------------------

    async def test_between_turns_output_is_delivered_then_turns_still_work(self) -> None:
        session, client = await self._start_session()
        session.set_unsolicited_handler(self._handler)

        # Autonomous assistant+result pair with NO active turn: the text is
        # buffered and delivered once, on the terminal ResultMessage (the
        # result carries no text of its own, proving accumulation).
        client.emit_assistant("background report")
        client.emit_result(result=None)
        await asyncio.wait_for(self.delivered_event.wait(), timeout=2.0)
        self.assertEqual(self.delivered, [("background report", client.session_id)])

        # A normal turn afterwards still streams through send_turn.
        events = await _collect(session.send_turn("hello"))
        texts = [event.text for event in events if isinstance(event, TextDeltaEvent)]
        self.assertEqual(texts, ["turn answer"])
        self.assertIsInstance(events[-1], CompletionEvent)
        self.assertEqual(len(self.delivered), 1)

    async def test_result_text_wins_over_buffered_assistant_text(self) -> None:
        session, client = await self._start_session()
        session.set_unsolicited_handler(self._handler)

        client.emit_stream_delta("partial ")
        client.emit_assistant("interim text")
        client.emit_result(result="final background text")
        await asyncio.wait_for(self.delivered_event.wait(), timeout=2.0)

        # Mirrors the direct path: ``msg.result or joined assistant texts``,
        # and stream partials are never delivered on the unsolicited route.
        self.assertEqual(
            self.delivered, [("final background text", client.session_id)]
        )

    async def test_mid_turn_frames_are_not_routed_to_the_handler(self) -> None:
        session, client = await self._start_session()
        session.set_unsolicited_handler(self._handler)

        events = await _collect(session.send_turn("hello"))

        texts = [event.text for event in events if isinstance(event, TextDeltaEvent)]
        self.assertEqual(texts, ["turn answer"])
        await self._drain(client)
        self.assertEqual(self.delivered, [])

    async def test_without_handler_frames_are_dropped_and_turns_unaffected(self) -> None:
        session, client = await self._start_session()

        client.emit_assistant("orphaned background text")
        client.emit_result(result=None)
        await self._drain(client)

        events = await _collect(session.send_turn("hello"))
        texts = [event.text for event in events if isinstance(event, TextDeltaEvent)]
        self.assertEqual(texts, ["turn answer"])

    async def test_handler_failure_never_breaks_the_reader_or_later_turns(self) -> None:
        session, client = await self._start_session()

        async def broken(text: str, session_id: str | None) -> None:
            raise RuntimeError("delivery route exploded")

        session.set_unsolicited_handler(broken)
        client.emit_assistant("background report")
        client.emit_result(result=None)
        await self._drain(client)

        events = await _collect(session.send_turn("hello"))
        texts = [event.text for event in events if isinstance(event, TextDeltaEvent)]
        self.assertEqual(texts, ["turn answer"])

        # Registration is replaceable: the next autonomous turn delivers.
        session.set_unsolicited_handler(self._handler)
        client.emit_assistant("second report")
        client.emit_result(result=None)
        await asyncio.wait_for(self.delivered_event.wait(), timeout=2.0)
        self.assertEqual(self.delivered, [("second report", client.session_id)])

    # -- ownership across interleaved turns --------------------------------

    async def test_inflight_autonomous_turn_keeps_frames_when_user_turn_arrives(
        self,
    ) -> None:
        session, client = await self._start_session()
        session.set_unsolicited_handler(self._handler)

        # The autonomous turn establishes ownership before any user turn.
        client.emit_assistant("autonomous progress")
        await _wait_until(lambda: session._unsolicited_inflight)

        # A user turn is submitted mid-autonomous-turn; its own frames are
        # controlled by hand ("hang" script).
        client.turn_scripts.append("hang")
        turn_task = asyncio.create_task(_collect(session.send_turn("user turn")))
        await _wait_until(lambda: client.queries == ["user turn"])

        # The autonomous turn's terminal result arrives AFTER the user turn
        # was submitted — the user turn must not steal it.
        client.emit_result(result=None)
        await asyncio.wait_for(self.delivered_event.wait(), timeout=2.0)
        self.assertEqual(self.delivered, [("autonomous progress", client.session_id)])

        # Only now do the user turn's frames arrive, and they belong to it.
        client.emit_assistant("user answer")
        client.emit_result(result="user answer")
        events = await asyncio.wait_for(turn_task, timeout=2.0)
        texts = [event.text for event in events if isinstance(event, TextDeltaEvent)]
        self.assertEqual(texts, ["user answer"])
        self.assertIsInstance(events[-1], CompletionEvent)
        self.assertEqual(len(self.delivered), 1)

    async def test_abandoned_turn_frames_are_swallowed_not_redelivered(self) -> None:
        session, client = await self._start_session()
        session.set_unsolicited_handler(self._handler)

        client.turn_scripts.append("hang")
        iterator = session.send_turn("will be abandoned").__aiter__()
        first_event = asyncio.create_task(iterator.__anext__())
        await _wait_until(lambda: client.queries == ["will be abandoned"])
        first_event.cancel()
        await asyncio.gather(first_event, return_exceptions=True)

        # The abandoned turn's late frames must be discarded through its
        # terminal result (the adapter counterpart of stall_swallow_result)…
        client.emit_assistant("late answer")
        client.emit_result(result="late answer")
        await self._drain(client)
        self.assertEqual(self.delivered, [])

        # …while a genuinely new autonomous turn afterwards still delivers.
        client.emit_assistant("fresh background report")
        client.emit_result(result=None)
        await asyncio.wait_for(self.delivered_event.wait(), timeout=2.0)
        self.assertEqual(
            self.delivered, [("fresh background report", client.session_id)]
        )

    async def test_abort_stalled_waiter_closes_owner_and_rotates_shared_lock(
        self,
    ) -> None:
        """#625: a waiter on another resumed session must not inherit the
        first session's permanently held turn lock after recovery."""

        owner, owner_client = await self._start_session()
        owner_client.turn_scripts.append("hang")
        owner_task = asyncio.create_task(_collect(owner.send_turn("owner")))
        await _wait_until(lambda: owner_client.queries == ["owner"])

        waiter = await self.runtime.start_or_resume(
            SessionRequest(
                working_directory="/workspace", session_id=owner.session_id
            )
        )
        waiter_client = self.clients[-1]
        waiter_task = asyncio.create_task(_collect(waiter.send_turn("waiter")))
        await asyncio.sleep(0.01)
        self.assertEqual(waiter_client.queries, [])

        await waiter.interrupt()
        waiter_task.cancel()
        await asyncio.gather(waiter_task, return_exceptions=True)
        await waiter.abort_stalled_turn()
        owner_events = await asyncio.wait_for(owner_task, timeout=2.0)

        self.assertEqual(owner_client.interrupts, 1)
        self.assertIsInstance(owner_events[-1], ErrorEvent)
        self.assertEqual(waiter_client.queries, [])

        recovered = await self.runtime.start_or_resume(
            SessionRequest(
                working_directory="/workspace", session_id=owner.session_id
            )
        )
        recovered_client = self.clients[-1]
        recovered_events = await asyncio.wait_for(
            _collect(recovered.send_turn("recovered")), timeout=2.0
        )
        self.assertEqual(recovered_client.queries, ["recovered"])
        self.assertIsInstance(recovered_events[-1], CompletionEvent)

    # -- raw SDK frame observation seam (#584 C-1 follow-up) ---------------

    async def test_frame_observer_sees_turn_unsolicited_and_rate_limit_frames(
        self,
    ) -> None:
        session, client = await self._start_session()
        session.set_unsolicited_handler(self._handler)
        frames: list[Message] = []
        session.set_sdk_frame_observer(frames.append)

        # Turn flow: the scripted assistant+result frames must be observed
        # while the turn's normalized event stream stays intact.
        events = await _collect(session.send_turn("hello"))
        texts = [event.text for event in events if isinstance(event, TextDeltaEvent)]
        self.assertEqual(texts, ["turn answer"])

        # Between-turns flow: rate-limit and unsolicited frames observed too.
        client.emit_rate_limit()
        client.emit_assistant("background report")
        client.emit_result(result=None)
        await asyncio.wait_for(self.delivered_event.wait(), timeout=2.0)
        await self._drain(client)

        self.assertEqual(
            [type(frame).__name__ for frame in frames],
            [
                "AssistantMessage",  # turn flow
                "ResultMessage",  # turn terminal
                "RateLimitEvent",  # account-level, no owning turn
                "AssistantMessage",  # unsolicited flow
                "ResultMessage",  # unsolicited terminal
            ],
        )
        # Observation-only: unsolicited delivery still happened exactly once.
        self.assertEqual(self.delivered, [("background report", client.session_id)])

    async def test_broken_frame_observer_never_affects_turns_or_delivery(
        self,
    ) -> None:
        session, client = await self._start_session()
        session.set_unsolicited_handler(self._handler)

        def broken_observer(_message: Message) -> None:
            raise RuntimeError("observer exploded")

        session.set_sdk_frame_observer(broken_observer)

        events = await _collect(session.send_turn("hello"))
        texts = [event.text for event in events if isinstance(event, TextDeltaEvent)]
        self.assertEqual(texts, ["turn answer"])
        self.assertIsInstance(events[-1], CompletionEvent)

        client.emit_rate_limit()
        client.emit_assistant("background report")
        client.emit_result(result=None)
        await asyncio.wait_for(self.delivered_event.wait(), timeout=2.0)
        self.assertEqual(self.delivered, [("background report", client.session_id)])


if __name__ == "__main__":
    unittest.main()
