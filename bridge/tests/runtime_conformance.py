"""Provider-neutral AgentRuntime conformance suite (#387).

This module is the executable behavior contract every ``AgentRuntime``
adapter must satisfy.  It is imported by ``test_runtime_conformance.py``
(which binds it to the real adapters plus a violating runtime) and is not a
test module itself.

Contract summary, enforced by :class:`AgentRuntimeConformanceSuite`:

* ``start_or_resume`` returns a session with a stable non-empty id and
  preserves a requested resume id.
* One turn is one event stream: it always ends, its final event is the
  single terminal event (``CompletionEvent`` or ``ErrorEvent``), and nothing
  follows the terminal event.
* A completed turn yields exactly one ``ResultEvent`` immediately before its
  ``CompletionEvent``; a failed turn yields none and its error carries a
  non-empty snake_case code, a message, and a boolean ``retryable``.
* Tool lifecycle events pair a started event with a completed event that
  shares the same call id and tool name.
* Approvals are fail-closed: omitting the handler or a handler that raises
  must reach the provider as a deny decision.
* ``interrupt()`` terminates an in-flight turn with error code
  ``interrupted`` and is a safe no-op on an idle session.
* Turns on one session never interleave: the provider sees the second turn
  only after the first reached its terminal event.
"""

from __future__ import annotations

import abc
import asyncio
from collections import deque
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack
import re
from typing import TYPE_CHECKING, Literal, TypeAlias
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
        MessageCompletedEvent,
        ModelInfo,
        ReasoningDeltaEvent,
        ResultEvent,
        SessionBrowser,
        SessionHistory,
        SessionHistoryMessage,
        SessionRequest,
        SessionSummary,
        TextDeltaEvent,
        ToolCompletedEvent,
        ToolStartedEvent,
        deny_approval,
    )
    from core.provider_capabilities import CapabilityState
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
        MessageCompletedEvent,
        ModelInfo,
        ReasoningDeltaEvent,
        ResultEvent,
        SessionBrowser,
        SessionHistory,
        SessionHistoryMessage,
        SessionRequest,
        SessionSummary,
        TextDeltaEvent,
        ToolCompletedEvent,
        ToolStartedEvent,
        deny_approval,
    )
    from telegram_bot.core.provider_capabilities import CapabilityState


DEFAULT_LIVENESS_TIMEOUT = 5.0
INTERRUPTED_ERROR_CODE = "interrupted"

SIMPLE_TURN_TEXT = "Hello, world"
TWO_MESSAGE_TEXTS = ("First answer.", "Second answer.")
TOOL_TURN_TEXT = "Done."
APPROVAL_TURN_TEXT = "After approval."
GATED_FIRST_TEXT = "Gated part."

# Capability axes this suite exercises.  test_provider_capabilities.py pins
# that a provider whose adapter binds this suite declares these axes
# `supported`, so the committed matrix and the executable coverage cannot
# drift apart silently.
CONFORMANCE_COVERED_AXES: tuple[str, ...] = (
    "runtime_adapter",
    "session_resume",
    "text_streaming",
    "message_boundaries",
    "tool_event_stream",
    "interactive_approvals",
    "turn_interrupt",
    "turn_serialization",
    "session_browsing",
    "model_discovery",
)

TurnScriptKind: TypeAlias = Literal[
    "simple",
    "two_messages",
    "tool",
    "approval",
    "failure",
    "hang",
    "gated",
]

# Violation switches understood by ReferenceAgentRuntime.  Each one breaks a
# distinct clause of the contract so the negative tests can prove the suite
# rejects a non-conformant runtime.
KNOWN_VIOLATIONS: frozenset[str] = frozenset(
    {
        "empty_session_id",
        "completion_before_result",
        "events_after_terminal",
        "missing_terminal",
        "auto_allow_by_default",
        "ignore_interrupt",
        "interleave_turns",
        "tool_completed_without_started",
    }
)

_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def visible_text(events: Sequence[AgentEvent]) -> str:
    """Concatenate the user-visible answer text of one turn."""

    return "".join(event.text for event in events if isinstance(event, TextDeltaEvent))


def visible_messages(events: Sequence[AgentEvent]) -> tuple[str, ...]:
    """Split user-visible text into messages at message-boundary events."""

    messages: list[str] = []
    current: list[str] = []
    for event in events:
        if isinstance(event, TextDeltaEvent):
            current.append(event.text)
        elif isinstance(event, MessageCompletedEvent) and current:
            messages.append("".join(current))
            current.clear()
    if current:
        messages.append("".join(current))
    return tuple(messages)


def _assert_tool_events_pair(events: Sequence[AgentEvent]) -> None:
    open_tools: dict[str, str] = {}
    for event in events:
        if isinstance(event, ToolStartedEvent):
            assert event.tool_call_id not in open_tools, (
                f"tool call {event.tool_call_id!r} started twice"
            )
            open_tools[event.tool_call_id] = event.tool_name
        elif isinstance(event, ToolCompletedEvent):
            assert event.tool_call_id in open_tools, (
                f"tool call {event.tool_call_id!r} completed without a start event"
            )
            assert open_tools.pop(event.tool_call_id) == event.tool_name, (
                f"tool call {event.tool_call_id!r} completed under a different name"
            )


def assert_turn_stream_contract(
    events: Sequence[AgentEvent],
    *,
    expect_success: bool | None = None,
) -> None:
    """Assert the normalized single-turn event-stream invariants."""

    assert events, "a turn must yield at least one event before its stream ends"
    terminal = events[-1]
    assert isinstance(terminal, (CompletionEvent, ErrorEvent)), (
        f"a turn stream must end with its terminal event, got {terminal.kind!r}"
    )
    for event in events[:-1]:
        assert not isinstance(event, (CompletionEvent, ErrorEvent)), (
            "no event may follow the terminal event of a turn"
        )
    result_indexes = [
        index for index, event in enumerate(events) if isinstance(event, ResultEvent)
    ]
    if isinstance(terminal, CompletionEvent):
        assert terminal.stop_reason, "a completion event must carry a stop reason"
        assert result_indexes == [len(events) - 2], (
            "a completed turn must yield exactly one ResultEvent immediately "
            f"before its CompletionEvent, got result indexes {result_indexes} "
            f"in {[event.kind for event in events]}"
        )
    else:
        assert not result_indexes, "a failed turn must not yield a ResultEvent"
        assert _ERROR_CODE_PATTERN.match(terminal.code), (
            f"error codes must be stable snake_case identifiers, got {terminal.code!r}"
        )
        assert terminal.message, "a terminal error must carry a message"
        assert isinstance(terminal.retryable, bool), "retryable must be a boolean"
    _assert_tool_events_pair(events)
    if expect_success is True:
        assert isinstance(terminal, CompletionEvent), (
            f"expected a successful turn, got error "
            f"{getattr(terminal, 'code', None)!r}: {getattr(terminal, 'message', None)!r}"
        )
    elif expect_success is False:
        assert isinstance(terminal, ErrorEvent), "expected the turn to fail"


class ConformanceHarness(abc.ABC):
    """Adapter-specific glue that lets the shared suite drive one provider.

    A harness owns a runtime wired to a deterministic fake transport, can
    prime the *next* turn of a session with one of the canonical scripts, and
    exposes what the provider observed (turn starts, approval decisions).
    """

    provider: str = "unknown"

    def __init__(self, *, liveness_timeout: float = DEFAULT_LIVENESS_TIMEOUT) -> None:
        self.liveness_timeout = liveness_timeout

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    @property
    @abc.abstractmethod
    def runtime(self) -> AgentRuntime: ...

    @abc.abstractmethod
    def capability_state(self, axis_key: str) -> CapabilityState: ...

    async def new_session(self) -> AgentSession:
        return await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace")
        )

    async def resume_session(self, session_id: str) -> AgentSession:
        return await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace", session_id=session_id)
        )

    @abc.abstractmethod
    def arrange_turn(self, session: AgentSession, kind: TurnScriptKind) -> None:
        """Prime the next ``send_turn`` on ``session`` with a canonical script."""

    @abc.abstractmethod
    def release_gated_turn(self) -> None:
        """Let a previously arranged ``gated`` turn run to completion."""

    @abc.abstractmethod
    def provider_turn_starts(self) -> Sequence[tuple[str, str]]:
        """Return ``(session_id, message)`` pairs the provider has started."""

    @abc.abstractmethod
    def provider_approval_decisions(self) -> Sequence[str]:
        """Return normalized ``allow``/``deny`` decisions the provider received."""

    @abc.abstractmethod
    def session_browser(self) -> SessionBrowser: ...

    @abc.abstractmethod
    def arrange_stored_sessions(self) -> str:
        """Prime stored-session browsing data; returns the stored session id."""

    async def wait_for_turn_started(self, count: int) -> None:
        async def poll() -> None:
            while len(self.provider_turn_starts()) < count:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(poll(), timeout=self.liveness_timeout)

    async def wait_until_interruptible(self, session: AgentSession) -> None:
        """Wait until an in-flight turn on ``session`` can be interrupted."""

        await self.wait_for_turn_started(len(self.provider_turn_starts()) or 1)


class _ReferenceSession:
    """The normative in-memory AgentSession used as the executable spec."""

    def __init__(self, runtime: ReferenceAgentRuntime, session_id: str) -> None:
        self._runtime = runtime
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        if "empty_session_id" in self._runtime.violations:
            return ""
        return self._session_id

    def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        async def events() -> AsyncIterator[AgentEvent]:
            runtime = self._runtime
            lock = runtime.session_lock(self._session_id)
            hold_lock = "interleave_turns" not in runtime.violations
            async with AsyncExitStack() as stack:
                if hold_lock:
                    await stack.enter_async_context(lock)
                script = runtime.begin_turn(self._session_id, message)
                async for event in runtime.script_events(
                    self._session_id, script, approval_handler
                ):
                    yield event

        return events()

    async def interrupt(self) -> None:
        if "ignore_interrupt" in self._runtime.violations:
            return
        self._runtime.signal_interrupt(self._session_id)


class ReferenceAgentRuntime:
    """Reference AgentRuntime implementation used as the conformance baseline.

    ``violations`` deliberately breaks one contract clause at a time so the
    negative tests can prove the suite fails a non-conformant runtime.
    """

    def __init__(self, violations: frozenset[str] = frozenset()) -> None:
        unknown = violations - KNOWN_VIOLATIONS
        if unknown:
            raise ValueError(f"unknown violations: {sorted(unknown)}")
        self.violations = violations
        self.turn_starts: list[tuple[str, str]] = []
        self.approval_decisions: list[str] = []
        self.gate = asyncio.Event()
        self._scripts: dict[str, deque[TurnScriptKind]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._interrupts: dict[str, asyncio.Event] = {}
        self._session_counter = 0
        self._stored_sessions: tuple[SessionSummary, ...] = ()
        self._stored_history: dict[str, SessionHistory] = {}

    # -- AgentRuntime protocol -------------------------------------------------

    async def start_or_resume(self, request: SessionRequest) -> AgentSession:
        if request.session_id is not None:
            return _ReferenceSession(self, request.session_id)
        self._session_counter += 1
        return _ReferenceSession(self, f"reference-session-{self._session_counter}")

    async def list_models(self) -> Sequence[ModelInfo]:
        return (
            ModelInfo(
                id="reference-model",
                display_name="Reference model",
                default_reasoning_effort="medium",
                supported_reasoning_efforts=("medium",),
                is_default=True,
            ),
        )

    # -- SessionBrowser protocol -----------------------------------------------

    @property
    def supports_session_browsing(self) -> bool:
        return True

    async def list_sessions(self, *, limit: int = 10) -> Sequence[SessionSummary]:
        return self._stored_sessions[:limit]

    async def read_session(self, session_id: str, *, limit: int = 5) -> SessionHistory:
        history = self._stored_history.get(session_id)
        if history is None:
            return SessionHistory(session_id, ())
        return SessionHistory(session_id, history.messages[-limit:])

    def store_sessions(
        self,
        sessions: Sequence[SessionSummary],
        history: dict[str, SessionHistory],
    ) -> None:
        self._stored_sessions = tuple(sessions)
        self._stored_history = dict(history)

    # -- scripting -------------------------------------------------------------

    def queue_script(self, session_id: str, kind: TurnScriptKind) -> None:
        self._scripts.setdefault(session_id, deque()).append(kind)

    def session_lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def begin_turn(self, session_id: str, message: str) -> TurnScriptKind:
        self._interrupts[session_id] = asyncio.Event()
        self.turn_starts.append((session_id, message))
        queued = self._scripts.get(session_id)
        if queued:
            return queued.popleft()
        return "simple"

    def signal_interrupt(self, session_id: str) -> None:
        event = self._interrupts.get(session_id)
        if event is not None:
            event.set()

    def script_events(
        self,
        session_id: str,
        script: TurnScriptKind,
        approval_handler: ApprovalHandler,
    ) -> AsyncIterator[AgentEvent]:
        if script == "two_messages":
            return self._two_message_events()
        if script == "tool":
            return self._tool_events()
        if script == "approval":
            return self._approval_events(approval_handler)
        if script == "failure":
            return self._failure_events()
        if script == "hang":
            return self._hang_events(session_id)
        if script == "gated":
            return self._gated_events()
        return self._simple_events()

    def _terminal_success(self) -> tuple[AgentEvent, ...]:
        result = ResultEvent(result={"status": "completed"})
        completion = CompletionEvent(stop_reason="end_turn")
        if "completion_before_result" in self.violations:
            return (completion, result)
        if "events_after_terminal" in self.violations:
            return (result, completion, TextDeltaEvent("late text after terminal"))
        if "missing_terminal" in self.violations:
            return (result,)
        return (result, completion)

    async def _simple_events(self) -> AsyncIterator[AgentEvent]:
        yield ReasoningDeltaEvent("planning the reply")
        yield TextDeltaEvent("Hello, ")
        yield TextDeltaEvent("world")
        yield MessageCompletedEvent()
        for event in self._terminal_success():
            yield event

    async def _two_message_events(self) -> AsyncIterator[AgentEvent]:
        for text in TWO_MESSAGE_TEXTS:
            yield TextDeltaEvent(text)
            yield MessageCompletedEvent()
        for event in self._terminal_success():
            yield event

    async def _tool_events(self) -> AsyncIterator[AgentEvent]:
        if "tool_completed_without_started" not in self.violations:
            yield ToolStartedEvent(
                tool_call_id="tool-1",
                tool_name="command",
                arguments={"command": "pwd"},
            )
        yield ToolCompletedEvent(
            tool_call_id="tool-1",
            tool_name="command",
            result={"exitCode": 0},
            success=True,
        )
        yield TextDeltaEvent(TOOL_TURN_TEXT)
        yield MessageCompletedEvent()
        for event in self._terminal_success():
            yield event

    async def _approval_events(
        self, approval_handler: ApprovalHandler
    ) -> AsyncIterator[AgentEvent]:
        request = ApprovalRequestEvent(
            request_id="approval-1",
            action="write_file",
            arguments={"path": "notes.txt"},
            description="Write notes.txt",
        )
        yield request
        if "auto_allow_by_default" in self.violations:
            decision = ApprovalDecision.ALLOW
        else:
            try:
                decision = await approval_handler(request)
            except asyncio.CancelledError:
                raise
            except Exception:
                decision = ApprovalDecision.DENY
        self.approval_decisions.append(decision.value)
        yield TextDeltaEvent(APPROVAL_TURN_TEXT)
        yield MessageCompletedEvent()
        for event in self._terminal_success():
            yield event

    async def _failure_events(self) -> AsyncIterator[AgentEvent]:
        yield TextDeltaEvent("partial answer before the failure")
        yield ErrorEvent(
            code="reference_provider_failure",
            message="Scripted provider failure",
            retryable=True,
        )

    async def _hang_events(self, session_id: str) -> AsyncIterator[AgentEvent]:
        yield TextDeltaEvent("working on it")
        if "ignore_interrupt" in self.violations:
            await asyncio.Event().wait()
        interrupt = self._interrupts[session_id]
        await interrupt.wait()
        yield ErrorEvent(code=INTERRUPTED_ERROR_CODE, message="Turn was interrupted")

    async def _gated_events(self) -> AsyncIterator[AgentEvent]:
        yield TextDeltaEvent(GATED_FIRST_TEXT)
        await self.gate.wait()
        yield MessageCompletedEvent()
        for event in self._terminal_success():
            yield event


# Static conformance: the reference runtime and session must satisfy the
# protocols exactly like a real adapter.
_reference_runtime_conforms: AgentRuntime = ReferenceAgentRuntime()
_reference_browser_conforms: SessionBrowser = ReferenceAgentRuntime()
_reference_session_conforms: AgentSession = _ReferenceSession(
    ReferenceAgentRuntime(), "typed-session"
)


class ReferenceConformanceHarness(ConformanceHarness):
    """Bind the conformance suite to the normative reference runtime."""

    provider = "reference"

    def __init__(
        self,
        *,
        violations: frozenset[str] = frozenset(),
        liveness_timeout: float = DEFAULT_LIVENESS_TIMEOUT,
    ) -> None:
        super().__init__(liveness_timeout=liveness_timeout)
        self._violations = violations
        self._runtime: ReferenceAgentRuntime | None = None

    async def start(self) -> None:
        self._runtime = ReferenceAgentRuntime(self._violations)

    async def close(self) -> None:
        self._runtime = None

    @property
    def runtime(self) -> AgentRuntime:
        assert self._runtime is not None, "harness not started"
        return self._runtime

    @property
    def reference_runtime(self) -> ReferenceAgentRuntime:
        assert self._runtime is not None, "harness not started"
        return self._runtime

    def capability_state(self, axis_key: str) -> CapabilityState:
        return CapabilityState.SUPPORTED

    def arrange_turn(self, session: AgentSession, kind: TurnScriptKind) -> None:
        self.reference_runtime.queue_script(session.session_id, kind)

    def release_gated_turn(self) -> None:
        self.reference_runtime.gate.set()

    def provider_turn_starts(self) -> Sequence[tuple[str, str]]:
        return tuple(self.reference_runtime.turn_starts)

    def provider_approval_decisions(self) -> Sequence[str]:
        return tuple(self.reference_runtime.approval_decisions)

    def session_browser(self) -> SessionBrowser:
        return self.reference_runtime

    def arrange_stored_sessions(self) -> str:
        stored_id = "reference-stored-1"
        summary = SessionSummary(
            id=stored_id,
            title="Stored session",
            preview="hello",
            updated_at=42.0,
            cwd="/workspace",
            model="reference-model",
        )
        history = SessionHistory(
            session_id=stored_id,
            messages=(
                SessionHistoryMessage(role="user", content="hello"),
                SessionHistoryMessage(role="assistant", content="hi"),
            ),
        )
        self.reference_runtime.store_sessions((summary,), {stored_id: history})
        return stored_id


class AgentRuntimeConformanceSuite(unittest.IsolatedAsyncioTestCase, abc.ABC):
    """Shared behavior contract; bind it to an adapter via ``make_harness``.

    Concrete subclasses live in ``test_*.py`` modules; this base is never
    collected because this module is not a test module.
    """

    @abc.abstractmethod
    def make_harness(self) -> ConformanceHarness: ...

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.harness = self.make_harness()
        await self.harness.start()
        self.addAsyncCleanup(self.harness.close)

    # -- helpers ---------------------------------------------------------------

    def _require(self, axis_key: str) -> None:
        state = self.harness.capability_state(axis_key)
        if state is not CapabilityState.SUPPORTED:
            self.skipTest(
                f"{self.harness.provider} declares {axis_key}={state.value}; "
                "behavioral conformance applies to supported axes only"
            )

    async def _collect(
        self,
        session: AgentSession,
        message: str,
        *,
        approval_handler: ApprovalHandler | None = None,
    ) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        if approval_handler is None:
            iterator = session.send_turn(message)
        else:
            iterator = session.send_turn(message, approval_handler=approval_handler)

        async def drain() -> None:
            async for event in iterator:
                events.append(event)

        try:
            await asyncio.wait_for(drain(), timeout=self.harness.liveness_timeout)
        except TimeoutError:
            raise AssertionError(
                "liveness violation: the turn stream did not end within "
                f"{self.harness.liveness_timeout}s; events so far: "
                f"{[event.kind for event in events]}"
            ) from None
        return events

    # -- session lifecycle -------------------------------------------------------

    async def test_new_session_exposes_stable_nonempty_session_id(self) -> None:
        session = await self.harness.new_session()
        self.assertIsInstance(session.session_id, str)
        self.assertTrue(session.session_id, "session ids must not be empty")
        self.assertEqual(session.session_id, session.session_id)

    async def test_resume_preserves_requested_session_id(self) -> None:
        self._require("session_resume")
        first = await self.harness.new_session()
        resumed = await self.harness.resume_session(first.session_id)
        self.assertEqual(resumed.session_id, first.session_id)

    # -- turn delivery -----------------------------------------------------------

    async def test_simple_turn_streams_text_then_result_then_completion(self) -> None:
        self._require("text_streaming")
        session = await self.harness.new_session()
        self.harness.arrange_turn(session, "simple")
        events = await self._collect(session, "hello")
        assert_turn_stream_contract(events, expect_success=True)
        self.assertEqual(visible_text(events), SIMPLE_TURN_TEXT)
        self.assertTrue(
            any(isinstance(event, TextDeltaEvent) for event in events),
            "answer text must stream as TextDeltaEvent increments",
        )

    async def test_turn_marks_intra_turn_message_boundaries(self) -> None:
        self._require("message_boundaries")
        session = await self.harness.new_session()
        self.harness.arrange_turn(session, "two_messages")
        events = await self._collect(session, "hello")
        assert_turn_stream_contract(events, expect_success=True)
        self.assertEqual(visible_messages(events), TWO_MESSAGE_TEXTS)

    async def test_tool_lifecycle_events_pair_within_the_turn(self) -> None:
        self._require("tool_event_stream")
        session = await self.harness.new_session()
        self.harness.arrange_turn(session, "tool")
        events = await self._collect(session, "run the tool")
        assert_turn_stream_contract(events, expect_success=True)
        started = [event for event in events if isinstance(event, ToolStartedEvent)]
        completed = [event for event in events if isinstance(event, ToolCompletedEvent)]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(completed), 1)
        self.assertEqual(started[0].tool_call_id, completed[0].tool_call_id)
        self.assertEqual(started[0].tool_name, completed[0].tool_name)
        self.assertLess(events.index(started[0]), events.index(completed[0]))
        self.assertEqual(visible_text(events), TOOL_TURN_TEXT)

    # -- approvals ---------------------------------------------------------------

    async def test_omitted_approval_handler_is_fail_closed_deny(self) -> None:
        self._require("interactive_approvals")
        session = await self.harness.new_session()
        self.harness.arrange_turn(session, "approval")
        events = await self._collect(session, "do something privileged")
        assert_turn_stream_contract(events)
        decisions = self.harness.provider_approval_decisions()
        self.assertEqual(
            list(decisions),
            ["deny"],
            "omitting the approval handler must reach the provider as deny",
        )

    async def test_approval_allow_reaches_provider_with_request_context(self) -> None:
        self._require("interactive_approvals")
        session = await self.harness.new_session()
        self.harness.arrange_turn(session, "approval")
        seen: list[ApprovalRequestEvent] = []

        async def allow(request: ApprovalRequestEvent) -> ApprovalDecision:
            seen.append(request)
            return ApprovalDecision.ALLOW

        events = await self._collect(session, "do something privileged", approval_handler=allow)
        assert_turn_stream_contract(events, expect_success=True)
        self.assertEqual(list(self.harness.provider_approval_decisions()), ["allow"])
        self.assertEqual(visible_text(events), APPROVAL_TURN_TEXT)
        stream_requests = [
            event for event in events if isinstance(event, ApprovalRequestEvent)
        ]
        self.assertEqual(len(stream_requests), 1)
        self.assertEqual(len(seen), 1)
        for request in (*stream_requests, *seen):
            self.assertTrue(request.request_id)
            self.assertTrue(request.action)
            self.assertTrue(request.description)

    async def test_failing_approval_handler_is_fail_closed_deny(self) -> None:
        self._require("interactive_approvals")
        session = await self.harness.new_session()
        self.harness.arrange_turn(session, "approval")

        async def broken(request: ApprovalRequestEvent) -> ApprovalDecision:
            raise RuntimeError("approval UI crashed")

        events = await self._collect(
            session, "do something privileged", approval_handler=broken
        )
        assert_turn_stream_contract(events)
        self.assertEqual(list(self.harness.provider_approval_decisions()), ["deny"])

    # -- cancel / errors ---------------------------------------------------------

    async def test_interrupt_terminates_inflight_turn_with_interrupted_code(self) -> None:
        self._require("turn_interrupt")
        session = await self.harness.new_session()
        self.harness.arrange_turn(session, "hang")
        collector = asyncio.ensure_future(self._collect(session, "never finishes"))
        try:
            await self.harness.wait_until_interruptible(session)
            await session.interrupt()
            events = await asyncio.wait_for(
                collector, timeout=self.harness.liveness_timeout
            )
        finally:
            if not collector.done():
                collector.cancel()
                await asyncio.gather(collector, return_exceptions=True)
        assert_turn_stream_contract(events, expect_success=False)
        terminal = events[-1]
        assert isinstance(terminal, ErrorEvent)
        self.assertEqual(terminal.code, INTERRUPTED_ERROR_CODE)

    async def test_interrupt_without_active_turn_is_a_noop(self) -> None:
        self._require("turn_interrupt")
        session = await self.harness.new_session()
        await session.interrupt()

    async def test_provider_failure_maps_to_normalized_terminal_error(self) -> None:
        session = await self.harness.new_session()
        self.harness.arrange_turn(session, "failure")
        events = await self._collect(session, "hello")
        assert_turn_stream_contract(events, expect_success=False)
        terminal = events[-1]
        assert isinstance(terminal, ErrorEvent)
        self.assertNotEqual(terminal.code, INTERRUPTED_ERROR_CODE)

    # -- serialization -----------------------------------------------------------

    async def test_concurrent_turns_on_one_session_serialize(self) -> None:
        self._require("turn_serialization")
        session = await self.harness.new_session()
        self.harness.arrange_turn(session, "gated")
        self.harness.arrange_turn(session, "simple")
        first = asyncio.ensure_future(self._collect(session, "first message"))
        try:
            await self.harness.wait_for_turn_started(1)
            second = asyncio.ensure_future(self._collect(session, "second message"))
            try:
                await asyncio.sleep(0.05)
                self.assertEqual(
                    len(self.harness.provider_turn_starts()),
                    1,
                    "the second turn must not reach the provider while the "
                    "first is in flight",
                )
                self.harness.release_gated_turn()
                first_events = await asyncio.wait_for(
                    first, timeout=self.harness.liveness_timeout
                )
                second_events = await asyncio.wait_for(
                    second, timeout=self.harness.liveness_timeout
                )
            finally:
                if not second.done():
                    second.cancel()
                    await asyncio.gather(second, return_exceptions=True)
        finally:
            if not first.done():
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
        assert_turn_stream_contract(first_events, expect_success=True)
        assert_turn_stream_contract(second_events, expect_success=True)
        self.assertEqual(visible_messages(first_events)[0], GATED_FIRST_TEXT)
        self.assertEqual(visible_text(second_events), SIMPLE_TURN_TEXT)
        self.assertEqual(
            [message for _sid, message in self.harness.provider_turn_starts()],
            ["first message", "second message"],
        )

    # -- discovery / browsing ------------------------------------------------------

    async def test_list_models_returns_normalized_models(self) -> None:
        self._require("model_discovery")
        models = await self.harness.runtime.list_models()
        self.assertGreaterEqual(len(models), 1)
        for model in models:
            self.assertIsInstance(model, ModelInfo)
            self.assertIsInstance(model.is_default, bool)
            self.assertIsInstance(model.supported_reasoning_efforts, tuple)

    async def test_session_browsing_is_bounded_and_normalized(self) -> None:
        self._require("session_browsing")
        stored_id = self.harness.arrange_stored_sessions()
        browser = self.harness.session_browser()
        self.assertTrue(browser.supports_session_browsing)
        summaries = await browser.list_sessions(limit=2)
        self.assertLessEqual(len(summaries), 2)
        self.assertTrue(summaries, "the arranged stored session must be listed")
        for summary in summaries:
            self.assertIsInstance(summary, SessionSummary)
            self.assertTrue(summary.id)
        history = await browser.read_session(stored_id, limit=1)
        self.assertIsInstance(history, SessionHistory)
        self.assertEqual(history.session_id, stored_id)
        self.assertLessEqual(len(history.messages), 1)
        for message in history.messages:
            self.assertIn(message.role, {"user", "assistant"})
            self.assertTrue(message.content)
