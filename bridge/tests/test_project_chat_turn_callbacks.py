"""Focused tests for the per-turn callback factories extracted in #896 PR2.

The stall/interrupt path used to be a closure inside ``_process_agent_message``
that wrote ``nonlocal approval_stall_won``; it is now built by
``_make_turn_interrupter`` and writes ``_TurnCallbacks.approval_stall_won``.
These tests pin the one behaviour the design comment flagged as easy to lose
in the move: when the approval-stall path loses the terminal race (a
concurrent ``/stop`` already claimed the request), ``interrupt_turn`` must raise
``CancelledError`` and touch nothing — not invalidate approvals, not cancel
callbacks, not interrupt the session. Forgetting the state cell would turn
``approval_stall_won`` into always-False and trip that branch on every stall.
"""

from __future__ import annotations

import asyncio

from telegram_bot.core.project_chat_process import (
    ProjectChatProcessMixin,
    _TurnCallbacks,
)
from telegram_bot.core.project_chat_turn_consumer import TurnStreamOutcome
from telegram_bot.core.project_chat_turn_state import TurnEventState
from telegram_bot.core.project_chat_types import _PendingRequest
from telegram_bot.core.request_lifecycle import RequestPhase


class _Host:
    """The slice of the mixin's surface interrupt_turn touches."""

    def __init__(self) -> None:
        self.invalidated: list[tuple[int, int]] = []
        self.interrupted: list[object] = []

    def invalidate_agent_approvals(self, user_id: int, chat_id: int) -> None:
        self.invalidated.append((user_id, chat_id))

    async def _interrupt_agent_session(self, session: object) -> None:
        self.interrupted.append(session)


def _request() -> _PendingRequest:
    loop = asyncio.get_running_loop()
    return _PendingRequest(
        user_id=1,
        chat_id=2,
        model=None,
        requested_session_id=None,
        permission_callback=None,
        typing_callback=None,
        future=loop.create_future(),
    )


def _interrupter(host: _Host, request: _PendingRequest, state: TurnEventState,
                 callbacks: _TurnCallbacks, active: set[asyncio.Task[object]]):
    return ProjectChatProcessMixin._make_turn_interrupter(
        host,  # type: ignore[arg-type]
        user_id=1,
        chat_id=2,
        session=object(),
        progress_request=request,
        turn_state=state,
        callbacks=callbacks,
        active_approval_callbacks=active,
    )


def test_lost_terminal_race_raises_cancelled_and_touches_nothing() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        # A concurrent /stop already owns the terminal phase.
        assert request.lifecycle.try_terminal(RequestPhase.CANCELED, cause="stop").won
        state = TurnEventState()
        state.approval_pending = True
        callbacks = _TurnCallbacks()
        interrupt = _interrupter(host, request, state, callbacks, set())

        raised = False
        try:
            await interrupt(TurnStreamOutcome.APPROVAL_STALL)
        except asyncio.CancelledError:
            raised = True
        assert raised, "losing the terminal race must surface as CancelledError"
        assert callbacks.approval_stall_won is False
        assert host.invalidated == []
        assert host.interrupted == []

    asyncio.run(run())


def test_won_terminal_race_records_stall_and_cancels_pending_callbacks() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        state = TurnEventState()
        state.approval_pending = True
        callbacks = _TurnCallbacks()

        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def pending_approval() -> None:
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(pending_approval())
        await started.wait()
        active: set[asyncio.Task[object]] = {task}
        interrupt = _interrupter(host, request, state, callbacks, active)

        await interrupt(TurnStreamOutcome.APPROVAL_STALL)

        assert callbacks.approval_stall_won is True
        assert request.lifecycle.phase is RequestPhase.TIMEOUT
        assert host.invalidated == [(1, 2)]
        assert cancelled.is_set() and task.cancelled()
        assert len(host.interrupted) == 1

    asyncio.run(run())


def test_no_pending_approval_only_interrupts_the_session() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        state = TurnEventState()
        callbacks = _TurnCallbacks()
        interrupt = _interrupter(host, request, state, callbacks, set())

        await interrupt(TurnStreamOutcome.TERMINAL_STALL)

        assert callbacks.approval_stall_won is False
        assert host.invalidated == []
        assert len(host.interrupted) == 1
        # The lifecycle is untouched: terminal ownership belongs to the caller.
        assert request.lifecycle.phase is not RequestPhase.TIMEOUT

    asyncio.run(run())
