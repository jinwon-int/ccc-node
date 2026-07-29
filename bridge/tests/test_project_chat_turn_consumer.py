"""Unit tests for the typed provider-neutral turn stream consumer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from telegram_bot.core.agent_runtime import (
    AgentEvent,
    ApprovalRequestEvent,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
)
from telegram_bot.core.project_chat_turn_consumer import (
    TurnEventDirective,
    TurnStreamOutcome,
    consume_turn_stream,
)
from telegram_bot.core.project_chat_turn_state import TurnEventState


_END = object()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ScriptedStream:
    def __init__(
        self,
        items: list[AgentEvent | object],
        *,
        order: list[str] | None = None,
    ) -> None:
        self.items = items
        self.order = order if order is not None else []
        self.closed = 0
        self.reads = 0

    def __aiter__(self) -> ScriptedStream:
        return self

    async def __anext__(self) -> AgentEvent:
        self.reads += 1
        if self.items:
            item = self.items.pop(0)
            if item is _END:
                raise StopAsyncIteration
            assert isinstance(
                item,
                (
                    ApprovalRequestEvent,
                    TextDeltaEvent,
                    ToolCompletedEvent,
                    ToolStartedEvent,
                ),
            )
            return item
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.order.append("pending-cancel")
            raise
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        self.closed += 1
        self.order.append("aclose")


class DelayedCancellationStream(ScriptedStream):
    def __init__(self, order: list[str]) -> None:
        super().__init__([], order=order)
        self.cancel_started = asyncio.Event()
        self.cancel_release = asyncio.Event()

    async def __anext__(self) -> AgentEvent:
        self.reads += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.order.append("pending-cancel-start")
            self.cancel_started.set()
            await self.cancel_release.wait()
            self.order.append("pending-cancel-done")
            raise
        raise AssertionError("unreachable")


def _no_action() -> Awaitable[None]:
    async def run() -> None:
        return None

    return run()


def _record_action(order: list[str], name: str) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        order.append(name)

    return run


def _observer(
    state: TurnEventState,
    seen: list[AgentEvent] | None = None,
) -> Callable[[AgentEvent, float], Awaitable[TurnEventDirective]]:
    async def observe(event: AgentEvent, now: float) -> TurnEventDirective:
        assert now >= 0
        if not state.admitted:
            state.mark_admitted()
        state.observe(event, observed_at=now)
        if seen is not None:
            seen.append(event)
        return TurnEventDirective.CONTINUE

    return observe


@pytest.mark.anyio
async def test_normal_eof_serializes_event_effects_and_closes() -> None:
    first = TextDeltaEvent("first")
    second = TextDeltaEvent("second")
    stream = ScriptedStream([first, second, _END])
    state = TurnEventState()
    seen: list[AgentEvent] = []

    outcome = await consume_turn_stream(
        stream,
        state=state,
        has_text=lambda: False,
        on_event=_observer(state, seen),
        interrupt=_no_action,
        abort_stalled_turn=None,
        admission_timeout_seconds=1.0,
        approval_stall_seconds=1.0,
        terminal_stall_seconds=1.0,
        interrupt_timeout_seconds=1.0,
    )

    assert outcome is TurnStreamOutcome.COMPLETED
    assert seen == [first, second]
    assert stream.closed == 1
    assert stream.order == ["aclose"]


@pytest.mark.anyio
async def test_completed_read_wins_before_admission_timeout_sample() -> None:
    event = TextDeltaEvent("ready")
    stream = ScriptedStream([event, _END])
    state = TurnEventState()
    seen: list[AgentEvent] = []
    interrupted = 0

    async def interrupt() -> None:
        nonlocal interrupted
        interrupted += 1

    outcome = await consume_turn_stream(
        stream,
        state=state,
        has_text=lambda: False,
        on_event=_observer(state, seen),
        interrupt=interrupt,
        abort_stalled_turn=None,
        admission_timeout_seconds=0.001,
        approval_stall_seconds=0.001,
        terminal_stall_seconds=0.001,
        interrupt_timeout_seconds=0.001,
    )

    assert outcome is TurnStreamOutcome.COMPLETED
    assert seen == [event]
    assert interrupted == 0


@pytest.mark.anyio
async def test_admission_timeout_preserves_interrupt_drain_abort_close_order() -> None:
    order: list[str] = []
    stream = ScriptedStream([], order=order)
    state = TurnEventState()

    outcome = await consume_turn_stream(
        stream,
        state=state,
        has_text=lambda: False,
        on_event=_observer(state),
        interrupt=_record_action(order, "interrupt"),
        abort_stalled_turn=_record_action(order, "abort"),
        admission_timeout_seconds=0.001,
        approval_stall_seconds=0.001,
        terminal_stall_seconds=0.001,
        interrupt_timeout_seconds=1.0,
    )

    assert outcome is TurnStreamOutcome.ADMISSION_TIMEOUT
    assert order == ["interrupt", "pending-cancel", "abort", "aclose"]


@pytest.mark.anyio
async def test_terminal_stall_uses_the_same_order_after_admission() -> None:
    order: list[str] = []
    stream = ScriptedStream([], order=order)
    state = TurnEventState()
    state.mark_admitted()

    outcome = await consume_turn_stream(
        stream,
        state=state,
        has_text=lambda: True,
        on_event=_observer(state),
        interrupt=_record_action(order, "interrupt"),
        abort_stalled_turn=_record_action(order, "abort"),
        admission_timeout_seconds=0.001,
        approval_stall_seconds=0.001,
        terminal_stall_seconds=0.001,
        interrupt_timeout_seconds=1.0,
    )

    assert outcome is TurnStreamOutcome.TERMINAL_STALL
    assert order == ["interrupt", "pending-cancel", "abort", "aclose"]


@pytest.mark.anyio
@pytest.mark.parametrize("suppression", ["tool", "approval"])
async def test_busy_or_approval_state_suppresses_terminal_stall(
    suppression: str,
) -> None:
    order: list[str] = []
    stream = ScriptedStream([], order=order)
    state = TurnEventState()
    state.mark_admitted()
    if suppression == "tool":
        state.observe(ToolStartedEvent("tool-1", "Bash", {"command": "sleep 1"}))
    else:
        state.observe(
            ApprovalRequestEvent(
                "approval-1",
                "Bash",
                {"command": "true"},
                "run command",
            ),
            observed_at=asyncio.get_running_loop().time(),
        )

    consumer = asyncio.create_task(
        consume_turn_stream(
            stream,
            state=state,
            has_text=lambda: True,
            on_event=_observer(state),
            interrupt=_record_action(order, "interrupt"),
            abort_stalled_turn=_record_action(order, "abort"),
            admission_timeout_seconds=0.001,
            approval_stall_seconds=0.005,
            terminal_stall_seconds=0.001,
            interrupt_timeout_seconds=1.0,
        )
    )

    if suppression == "approval":
        assert await consumer is TurnStreamOutcome.APPROVAL_STALL
        assert order == ["interrupt", "pending-cancel", "abort", "aclose"]
        return

    # A running tool still suppresses the short terminal-text guard and is not
    # subject to the approval deadline. It remains owned by the outer process
    # timeout (or /stop), which is deliberately a separate, longer contract.
    await asyncio.sleep(0.01)
    assert not consumer.done()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert order == ["pending-cancel", "aclose"]


@pytest.mark.anyio
async def test_stall_arms_after_active_tool_is_released() -> None:
    order: list[str] = []
    stream = ScriptedStream(
        [ToolCompletedEvent("tool-1", "Bash", result=None, success=True)],
        order=order,
    )
    state = TurnEventState()
    state.mark_admitted()
    state.observe(ToolStartedEvent("tool-1", "Bash", {"command": "true"}))

    outcome = await consume_turn_stream(
        stream,
        state=state,
        has_text=lambda: True,
        on_event=_observer(state),
        interrupt=_record_action(order, "interrupt"),
        abort_stalled_turn=_record_action(order, "abort"),
        admission_timeout_seconds=0.001,
        approval_stall_seconds=0.001,
        terminal_stall_seconds=0.001,
        interrupt_timeout_seconds=1.0,
    )

    assert outcome is TurnStreamOutcome.TERMINAL_STALL
    assert order == ["interrupt", "pending-cancel", "abort", "aclose"]


@pytest.mark.anyio
async def test_event_effect_exception_is_not_converted_to_an_outcome() -> None:
    stream = ScriptedStream([TextDeltaEvent("boom")])
    state = TurnEventState()

    async def fail(_event: AgentEvent, _now: float) -> TurnEventDirective:
        raise RuntimeError("effect failed")

    with pytest.raises(RuntimeError, match="effect failed"):
        await consume_turn_stream(
            stream,
            state=state,
            has_text=lambda: False,
            on_event=fail,
            interrupt=_no_action,
            abort_stalled_turn=None,
            admission_timeout_seconds=1.0,
            approval_stall_seconds=1.0,
            terminal_stall_seconds=1.0,
            interrupt_timeout_seconds=1.0,
        )

    assert stream.closed == 1


@pytest.mark.anyio
async def test_event_effect_cancellation_propagates_and_closes() -> None:
    stream = ScriptedStream([TextDeltaEvent("cancel")])
    state = TurnEventState()

    async def cancel(_event: AgentEvent, _now: float) -> TurnEventDirective:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await consume_turn_stream(
            stream,
            state=state,
            has_text=lambda: False,
            on_event=cancel,
            interrupt=_no_action,
            abort_stalled_turn=None,
            admission_timeout_seconds=1.0,
            approval_stall_seconds=1.0,
            terminal_stall_seconds=1.0,
            interrupt_timeout_seconds=1.0,
        )

    assert stream.closed == 1


@pytest.mark.anyio
async def test_stop_directive_completes_and_closes_without_reading_again() -> None:
    stream = ScriptedStream([TextDeltaEvent("stop")])
    state = TurnEventState()

    async def stop(_event: AgentEvent, _now: float) -> TurnEventDirective:
        return TurnEventDirective.STOP

    outcome = await consume_turn_stream(
        stream,
        state=state,
        has_text=lambda: False,
        on_event=stop,
        interrupt=_no_action,
        abort_stalled_turn=None,
        admission_timeout_seconds=1.0,
        approval_stall_seconds=1.0,
        terminal_stall_seconds=1.0,
        interrupt_timeout_seconds=1.0,
    )

    assert outcome is TurnStreamOutcome.COMPLETED
    assert stream.reads == 1
    assert stream.closed == 1


@pytest.mark.anyio
async def test_abort_failure_is_logged_and_iterator_still_closes() -> None:
    order: list[str] = []
    stream = ScriptedStream([], order=order)
    state = TurnEventState()

    async def abort() -> None:
        order.append("abort")
        raise RuntimeError("abort failed")

    outcome = await consume_turn_stream(
        stream,
        state=state,
        has_text=lambda: False,
        on_event=_observer(state),
        interrupt=_record_action(order, "interrupt"),
        abort_stalled_turn=abort,
        admission_timeout_seconds=0.001,
        approval_stall_seconds=0.001,
        terminal_stall_seconds=0.001,
        interrupt_timeout_seconds=1.0,
    )

    assert outcome is TurnStreamOutcome.ADMISSION_TIMEOUT
    assert order == ["interrupt", "pending-cancel", "abort", "aclose"]


@pytest.mark.anyio
async def test_repeated_consumer_cancellation_reaps_read_before_close() -> None:
    order: list[str] = []
    stream = DelayedCancellationStream(order)
    state = TurnEventState()
    consumer = asyncio.create_task(
        consume_turn_stream(
            stream,
            state=state,
            has_text=lambda: False,
            on_event=_observer(state),
            interrupt=_record_action(order, "interrupt"),
            abort_stalled_turn=_record_action(order, "abort"),
            admission_timeout_seconds=60.0,
            approval_stall_seconds=60.0,
            terminal_stall_seconds=60.0,
            interrupt_timeout_seconds=1.0,
        )
    )
    while stream.reads == 0:
        await asyncio.sleep(0)

    consumer.cancel()
    await stream.cancel_started.wait()
    consumer.cancel()
    await asyncio.sleep(0)
    assert "aclose" not in order

    stream.cancel_release.set()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert order == [
        "pending-cancel-start",
        "pending-cancel-done",
        "aclose",
    ]
    assert stream.closed == 1
