"""Typed event-stream consumption for one provider-neutral agent turn."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from telegram_bot.core.agent_runtime import AgentEvent
from telegram_bot.core.project_chat_event_stream import (
    EventWaitTimeout,
    TimeoutPreservingEventReader,
)
from telegram_bot.core.project_chat_turn_state import TurnEventState


logger = logging.getLogger(__name__)


class ClosableAgentEventStream(Protocol):
    """The normalized turn stream consumed and then explicitly closed."""

    def __aiter__(self) -> ClosableAgentEventStream: ...

    async def __anext__(self) -> AgentEvent: ...

    async def aclose(self) -> None: ...


class TurnEventDirective(str, Enum):
    """Tell the consumer whether the caller still accepts stream events."""

    CONTINUE = "continue"
    STOP = "stop"


class TurnStreamOutcome(str, Enum):
    """Why the event consumer returned to the request orchestrator."""

    COMPLETED = "completed"
    ADMISSION_TIMEOUT = "admission-timeout"
    APPROVAL_STALL = "approval-stall"
    TERMINAL_STALL = "terminal-stall"
    DELEGATED_TASK_STALL = "delegated-task-stall"


TurnEventEffect = Callable[[AgentEvent, float], Awaitable[TurnEventDirective]]
AsyncAction = Callable[[], Awaitable[None]]
TimeoutAction = Callable[[TurnStreamOutcome], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _TimeoutSelection:
    seconds: float
    outcome: TurnStreamOutcome


@dataclass(frozen=True, slots=True)
class _BoundedRead:
    event: AgentEvent | None
    timed_out: bool


def _select_timeout(
    *,
    state: TurnEventState,
    has_text: bool,
    now: float,
    admission_timeout_seconds: float,
    approval_stall_seconds: float,
    terminal_stall_seconds: float,
    delegated_task_stall_seconds: float,
) -> _TimeoutSelection | None:
    if not state.admitted and admission_timeout_seconds > 0:
        return _TimeoutSelection(
            admission_timeout_seconds,
            TurnStreamOutcome.ADMISSION_TIMEOUT,
        )
    candidates: list[_TimeoutSelection] = []
    if state.approval_pending and approval_stall_seconds > 0:
        pending_since = state.approval_pending_since
        elapsed = max(0.0, now - pending_since) if pending_since is not None else 0.0
        candidates.append(
            _TimeoutSelection(
                max(0.0, approval_stall_seconds - elapsed),
                TurnStreamOutcome.APPROVAL_STALL,
            )
        )
    if state.delegated_tasks_active > 0 and delegated_task_stall_seconds > 0:
        oldest_started_at = state.delegated_tasks_oldest_started_at
        elapsed = max(0.0, now - oldest_started_at) if oldest_started_at is not None else 0.0
        candidates.append(
            _TimeoutSelection(
                max(0.0, delegated_task_stall_seconds - elapsed),
                TurnStreamOutcome.DELEGATED_TASK_STALL,
            )
        )
    if (
        terminal_stall_seconds > 0
        and has_text
        and state.busy_depth <= 0
        and not state.approval_pending
        and state.delegated_tasks_active <= 0
    ):
        started_at = state.terminal_stall_started_at
        elapsed = max(0.0, now - started_at) if started_at is not None else 0.0
        candidates.append(
            _TimeoutSelection(
                max(0.0, terminal_stall_seconds - elapsed),
                TurnStreamOutcome.TERMINAL_STALL,
            )
        )
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: candidate.seconds)


async def _abort_stalled_owner(
    abort_stalled_turn: AsyncAction | None,
    *,
    timeout_seconds: float,
) -> None:
    if abort_stalled_turn is None:
        return
    try:
        await asyncio.wait_for(
            abort_stalled_turn(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.warning(
            "Agent stalled-turn abort timed out after %.1fs",
            timeout_seconds,
        )
    except Exception:
        logger.exception("Failed to abort stalled agent turn owner")


async def _read_bounded_event(
    reader: TimeoutPreservingEventReader[AgentEvent],
    *,
    timeout_seconds: float,
    timeout_outcome: TurnStreamOutcome,
    interrupt: TimeoutAction,
    abort_stalled_turn: AsyncAction | None,
    interrupt_timeout_seconds: float,
) -> _BoundedRead:
    try:
        event = await reader.read(timeout_seconds)
    except EventWaitTimeout:
        # The provider owns the blocked turn. Interrupt it before cancelling
        # the retained read that may be holding the provider's shared lock.
        await interrupt(timeout_outcome)
        await reader.cancel_pending()
        await _abort_stalled_owner(
            abort_stalled_turn,
            timeout_seconds=interrupt_timeout_seconds,
        )
        return _BoundedRead(event=None, timed_out=True)
    return _BoundedRead(event=event, timed_out=False)


async def consume_turn_stream(
    events: ClosableAgentEventStream,
    *,
    state: TurnEventState,
    has_text: Callable[[], bool],
    on_event: TurnEventEffect,
    interrupt: TimeoutAction,
    abort_stalled_turn: AsyncAction | None,
    admission_timeout_seconds: float,
    approval_stall_seconds: float,
    terminal_stall_seconds: float,
    interrupt_timeout_seconds: float,
    delegated_task_stall_seconds: float = 7200.0,
) -> TurnStreamOutcome:
    """Consume a turn without acquiring lifecycle, ledger, or delivery authority.

    A completed retained read wins over its timeout sample inside
    :class:`TimeoutPreservingEventReader`. Event effects stay serialized in the
    caller-provided callback. Exceptions, including cancellation, propagate and
    are never converted into a stream outcome.
    """

    reader = TimeoutPreservingEventReader[AgentEvent](events)
    try:
        while True:
            timeout = _select_timeout(
                state=state,
                has_text=has_text(),
                now=asyncio.get_running_loop().time(),
                admission_timeout_seconds=admission_timeout_seconds,
                approval_stall_seconds=approval_stall_seconds,
                terminal_stall_seconds=terminal_stall_seconds,
                delegated_task_stall_seconds=delegated_task_stall_seconds,
            )
            try:
                if timeout is None:
                    event = await events.__anext__()
                else:
                    read = await _read_bounded_event(
                        reader,
                        timeout_seconds=timeout.seconds,
                        timeout_outcome=timeout.outcome,
                        interrupt=interrupt,
                        abort_stalled_turn=abort_stalled_turn,
                        interrupt_timeout_seconds=interrupt_timeout_seconds,
                    )
                    if read.timed_out:
                        return timeout.outcome
                    assert read.event is not None
                    event = read.event
            except StopAsyncIteration:
                return TurnStreamOutcome.COMPLETED

            directive = await on_event(
                event,
                asyncio.get_running_loop().time(),
            )
            if directive is TurnEventDirective.STOP:
                return TurnStreamOutcome.COMPLETED
    finally:
        await reader.cancel_pending()
        try:
            await events.aclose()
        except Exception:
            pass


__all__ = [
    "AsyncAction",
    "ClosableAgentEventStream",
    "TurnEventDirective",
    "TurnEventEffect",
    "TimeoutAction",
    "TurnStreamOutcome",
    "consume_turn_stream",
]
