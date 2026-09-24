"""Focused tests for the outcome helpers extracted in #896 PR3.

``_process_agent_message`` used to classify the ``TurnStreamOutcome`` inline;
the four stall branches now live in ``_resolve_turn_outcome`` and the
``COMPLETED`` tail in ``_finish_completed_turn``. The existing stall /
timeout / empty-completion suites still drive these through the whole turn;
these tests call each helper directly with only a ``TurnEventState``, a
``TurnOutputBuffer`` and a ``_PendingRequest`` so every branch is reachable
without a runtime, and pin the two behaviours the move could silently lose:
the approval-stall defence must still *raise* ``CancelledError`` (never
return), and the terminal phase claimed for each branch must not change.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from telegram_bot.contracts.agent_runtime import ErrorEvent
from telegram_bot.core.project_chat_output import TurnOutputBuffer
from telegram_bot.core.project_chat_process import (
    _EMPTY_COMPLETION_MARKER,
    ProjectChatProcessMixin,
    _TurnCallbacks,
)
from telegram_bot.core.project_chat_turn_consumer import TurnStreamOutcome
from telegram_bot.core.project_chat_turn_state import TurnEventState
from telegram_bot.core.project_chat_types import _PendingRequest
from telegram_bot.core.request_lifecycle import RequestPhase
from telegram_bot.core.sdk_text import TERMINAL_STALL_NOTICE


class _Config:
    agent_provider = "claude"


class _Session:
    session_id = "sess-1"


class _Host:
    """The slice of the mixin's surface the outcome helpers touch."""

    def __init__(self, *, followers: int = 0) -> None:
        self._config = _Config()
        self.dropped: list[tuple[str, object]] = []
        self.cancelled: list[str] = []
        self._followers = followers

    async def _drop_agent_session(self, key: str, session: object) -> None:
        self.dropped.append((key, session))

    async def _cancel_agent_streaming(self, handler: object, *, context: str) -> None:
        self.cancelled.append(context)

    def _clean_response(self, text: str) -> str:
        return text.strip()

    def _conversation_followers(self, key: str) -> int:
        return self._followers


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


def _output(text: str = "") -> TurnOutputBuffer:
    output = TurnOutputBuffer()
    if text:
        output.append_delta(text)
    return output


async def _resolve(host: _Host, outcome: TurnStreamOutcome, request: _PendingRequest,
                   *, state: TurnEventState | None = None,
                   output: TurnOutputBuffer | None = None,
                   callbacks: _TurnCallbacks | None = None,
                   session: Any = None) -> Any:
    return await ProjectChatProcessMixin._resolve_turn_outcome(
        host,
        turn_outcome=outcome,
        key="1:2",
        session=session or _Session(),
        model=None,
        loop=asyncio.get_running_loop(),
        progress_request=request,
        streaming_handler=None,
        output=output or _output(),
        turn_state=state or TurnEventState(),
        callbacks=callbacks or _TurnCallbacks(),
        user_id=1,
        chat_id=2,
        admission_grace=5.0,
        approval_grace=7.0,
        stall_grace=9.0,
        delegated_stall_grace=11.0,
    )


async def _finish(host: _Host, request: _PendingRequest, *,
                  state: TurnEventState | None = None,
                  output: TurnOutputBuffer | None = None,
                  session: Any = None) -> Any:
    return await ProjectChatProcessMixin._finish_completed_turn(
        host,
        key="1:2",
        session=session or _Session(),
        runtime=object(),
        progress_request=request,
        streaming_handler=None,
        output=output or _output(),
        turn_state=state or TurnEventState(),
        user_id=1,
        chat_id=2,
    )


# --- _resolve_turn_outcome -------------------------------------------------


def test_completed_is_not_a_stall_and_falls_through() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        assert await _resolve(host, TurnStreamOutcome.COMPLETED, request) is None
        assert host.dropped == []
        assert not request.lifecycle.is_terminal

    asyncio.run(run())


def test_admission_timeout_claims_timeout_and_drops_the_session() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        session = _Session()
        response = await _resolve(
            host, TurnStreamOutcome.ADMISSION_TIMEOUT, request, session=session
        )
        assert response.success is False
        assert "did not start within 5s" in response.error
        assert response.failure_class == "admission-timeout/silent"
        assert response.session_id == "sess-1"
        assert request.lifecycle.phase is RequestPhase.TIMEOUT
        assert host.dropped == [("1:2", session)]

    asyncio.run(run())


def test_admission_timeout_lost_race_keeps_the_session() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        assert request.lifecycle.try_terminal(RequestPhase.CANCELED, cause="stop").won
        response = await _resolve(host, TurnStreamOutcome.ADMISSION_TIMEOUT, request)
        assert response.success is False
        assert host.dropped == []
        assert request.lifecycle.phase is RequestPhase.CANCELED

    asyncio.run(run())


def test_approval_stall_without_terminal_claim_raises_cancelled() -> None:
    # The interrupter claims the terminal phase before any abort effect; if
    # the stall did not win that race a concurrent /stop owns the turn and
    # the resolver must propagate CancelledError, not answer the user.
    async def run() -> None:
        host = _Host()
        request = _request()
        callbacks = _TurnCallbacks()
        callbacks.approval_stall_won = False
        with pytest.raises(asyncio.CancelledError):
            await _resolve(host, TurnStreamOutcome.APPROVAL_STALL, request,
                           callbacks=callbacks)
        assert host.dropped == []
        assert host.cancelled == []

    asyncio.run(run())


def test_approval_stall_names_the_pending_request_and_stops_streaming() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        callbacks = _TurnCallbacks()
        callbacks.approval_stall_won = True
        state = TurnEventState()
        state.approval_pending = True
        state.approval_pending_requests["req-1"] = "Bash (command)"
        session = _Session()
        response = await _resolve(host, TurnStreamOutcome.APPROVAL_STALL, request,
                                  state=state, callbacks=callbacks, session=session)
        assert response.success is False
        assert response.error == "Approval was not resolved within 7s (pending: Bash (command))"
        assert host.dropped == [("1:2", session)]
        assert host.cancelled == ["handling an approval-stall timeout"]

    asyncio.run(run())


def test_terminal_stall_delivers_partial_text_with_the_notice() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        response = await _resolve(host, TurnStreamOutcome.TERMINAL_STALL, request,
                                  output=_output("partial answer"))
        assert response.success is False
        assert response.content == f"partial answer\n\n{TERMINAL_STALL_NOTICE}"
        assert response.error == "Agent stopped before terminal completion"
        assert response.streamed is False
        assert request.lifecycle.phase is RequestPhase.INTERRUPTED
        assert len(host.dropped) == 1

    asyncio.run(run())


def test_terminal_stall_without_text_reports_no_response() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        response = await _resolve(host, TurnStreamOutcome.TERMINAL_STALL, request)
        assert response.content.startswith("(No response)\n\n")

    asyncio.run(run())


def test_delegated_task_stall_claims_interrupted_and_stops_streaming() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        state = TurnEventState()
        state.delegated_tasks_active = 2
        response = await _resolve(host, TurnStreamOutcome.DELEGATED_TASK_STALL,
                                  request, state=state)
        assert response.success is False
        assert response.error == "Delegated work exceeded its maximum runtime"
        assert request.lifecycle.phase is RequestPhase.INTERRUPTED
        assert len(host.dropped) == 1
        assert host.cancelled == ["handling a delegated-task stall"]

    asyncio.run(run())


# --- _finish_completed_turn ------------------------------------------------


def test_normal_completion_claims_completed() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        response = await _finish(host, request, output=_output("  hello  "))
        assert response.success is True
        assert response.content == "hello"
        assert response.streamed is False
        assert request.lifecycle.phase is RequestPhase.COMPLETED
        assert host.dropped == []

    asyncio.run(run())


def test_terminal_error_is_a_typed_failure_and_drops_the_session() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        state = TurnEventState()
        state.terminal_error = ErrorEvent(code="tool_use", message="boom", retryable=True)
        session = _Session()
        response = await _finish(host, request, state=state,
                                 output=_output("draft"), session=session)
        assert response.success is False
        assert response.content == "❌ Processing failed: boom"
        assert response.failure_code == "tool_use"
        # The error was never part of the draft: always deliver it.
        assert response.streamed is False
        assert request.lifecycle.phase is RequestPhase.FAILED
        assert host.dropped == [("1:2", session)]

    asyncio.run(run())


def test_danso_pause_is_classified_not_failed_text() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        state = TurnEventState()
        state.terminal_error = ErrorEvent(code="danso_task_paused", message="paused")
        response = await _finish(host, request, state=state)
        assert response.content == "⏸ paused"
        assert response.failure_class == "danso_task_paused"
        assert response.failure_code == "danso_task_paused"

    asyncio.run(run())


def test_empty_completion_recovers_the_terminal_result_text() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        state = TurnEventState()
        state.terminal_result_text = " final answer "
        response = await _finish(host, request, state=state)
        assert response.success is True
        assert response.content == "final answer"
        assert request.lifecycle.phase is RequestPhase.COMPLETED

    asyncio.run(run())


def test_empty_completion_with_a_follower_is_coalesced() -> None:
    async def run() -> None:
        host = _Host(followers=1)
        request = _request()
        response = await _finish(host, request)
        assert response.success is False
        assert response.error == "coalesced_turn"
        assert response.failure_class == "coalesced-turn"
        assert request.lifecycle.phase is RequestPhase.FAILED
        assert host.dropped == []  # session kept

    asyncio.run(run())


def test_empty_completion_without_followers_is_retryable() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        response = await _finish(host, request)
        assert response.success is False
        assert response.failure_class == _EMPTY_COMPLETION_MARKER
        assert request.lifecycle.phase is RequestPhase.FAILED
        assert host.dropped == []

    asyncio.run(run())
