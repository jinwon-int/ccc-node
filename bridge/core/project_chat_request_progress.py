"""Typed progress-task ownership for one provider-neutral ProjectChat request."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol

from telegram_bot.core.project_chat_types import (
    StatusCallback,
    TypingCallback,
    _PendingRequest,
)
from telegram_bot.core.request_lifecycle import RequestPhase


class LedgerCreate(Protocol):
    def __call__(
        self,
        user_id: int,
        chat_id: int,
        *,
        initial_state: str,
    ) -> str | None: ...


class DurationLogAppend(Protocol):
    def __call__(
        self,
        request: _PendingRequest,
        *,
        session_id: str | None,
        duration_ms: int,
        success: bool,
    ) -> None: ...


class LedgerFinish(Protocol):
    def __call__(
        self,
        request: _PendingRequest,
        state: str,
        *,
        cleanup_done: bool,
    ) -> None: ...


ProgressLoop = Callable[[_PendingRequest], Coroutine[Any, Any, None]]
HeartbeatCleanup = Callable[[_PendingRequest], Awaitable[bool]]


async def _reap_progress_task(
    task: asyncio.Task[None],
    *,
    timeout_seconds: float,
) -> None:
    try:
        await asyncio.wait_for(task, timeout=timeout_seconds)
    except TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is None or current.cancelling() or not task.cancelled():
            raise


@dataclass(slots=True)
class RequestProgressHandle:
    """The minimal progress resources attached to one request."""

    request: _PendingRequest
    task: asyncio.Task[None]
    _effects_started: bool = field(default=False, init=False, repr=False)


class RequestProgressCoordinator:
    """Start and reap progress resources without deciding request lifecycle."""

    def __init__(
        self,
        *,
        ledger_create: LedgerCreate,
        progress_loop: ProgressLoop,
        cleanup_heartbeat: HeartbeatCleanup,
        append_duration_log: DurationLogAppend,
        ledger_finish: LedgerFinish,
        reap_timeout_seconds: float = 5.0,
    ) -> None:
        if reap_timeout_seconds <= 0:
            raise ValueError("reap_timeout_seconds must be positive")
        self._ledger_create = ledger_create
        self._progress_loop = progress_loop
        self._cleanup_heartbeat = cleanup_heartbeat
        self._append_duration_log = append_duration_log
        self._ledger_finish = ledger_finish
        self._reap_timeout_seconds = reap_timeout_seconds

    def start(
        self,
        *,
        user_id: int,
        chat_id: int,
        model: str | None,
        requested_session_id: str | None,
        typing_callback: TypingCallback | None,
        status_callback: StatusCallback | None,
        streaming_handler: Any | None,
        usage_mode: str,
    ) -> RequestProgressHandle:
        """Create one request and its visible-progress task."""

        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        request = _PendingRequest(
            user_id=user_id,
            chat_id=chat_id,
            model=model,
            requested_session_id=requested_session_id,
            permission_callback=None,
            typing_callback=typing_callback,
            future=future,
            status_callback=status_callback,
            streaming_handler=streaming_handler,
        )
        request.usage_mode = usage_mode
        request.started_at = loop.time()
        request.task_id = self._ledger_create(
            user_id,
            chat_id,
            initial_state=RequestPhase.WAITING_FOR_TURN.value,
        )
        task: asyncio.Task[None] = asyncio.create_task(
            self._progress_loop(request),
            name=f"agent-progress-{user_id}-{chat_id}",
        )
        return RequestProgressHandle(request=request, task=task)

    async def finalize(
        self,
        handle: RequestProgressHandle,
        *,
        terminal_outcome: RequestPhase,
        session_id: str | None,
        duration_ms: int,
    ) -> None:
        """Run already-authorized finalization effects exactly once, in order."""

        if handle._effects_started:
            raise RuntimeError("request progress finalization already started")
        handle._effects_started = True

        request = handle.request
        if not request.future.done():
            request.future.set_result(None)
        await _reap_progress_task(
            handle.task,
            timeout_seconds=self._reap_timeout_seconds,
        )

        cleaned = await self._cleanup_heartbeat(request)
        await asyncio.to_thread(
            self._append_duration_log,
            request,
            session_id=session_id,
            duration_ms=max(0, int(duration_ms)),
            success=terminal_outcome is RequestPhase.COMPLETED,
        )
        self._ledger_finish(
            request,
            terminal_outcome.value,
            cleanup_done=cleaned,
        )


__all__ = [
    "DurationLogAppend",
    "HeartbeatCleanup",
    "LedgerCreate",
    "LedgerFinish",
    "ProgressLoop",
    "RequestProgressCoordinator",
    "RequestProgressHandle",
]
